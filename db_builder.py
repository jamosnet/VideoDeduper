import os
import sqlite3
import json
import subprocess
import pickle
from multiprocessing import Pool, cpu_count, freeze_support
from tqdm import tqdm
import sys
import json

# ================= 动态配置加载 =================
# 1. 设置默认安全回退值 (防止你直接双击运行脚本报错)
CFG = {
    "SOURCE_DIR": "E:\\vod",
    "DB_FILE": "video_library.db",
    "MAX_PROCESSES": 3,
    "AUDIO_THRESHOLD": 0.5,
    "VISUAL_COVERAGE": 0.6,
    "HAMMING_TOLERANCE": 9,
    "SAFE_DURATION_DIFF": 30
}

# 2. 接收来自主控 GUI 传过来的任务专属 config.json 路径
if len(sys.argv) > 1 and sys.argv[1].endswith('.json'):
    config_path = sys.argv[1]
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                CFG.update(json.load(f))
        except Exception as e:
            print(f"️[Warn] 无法读取工作区配置: {e}")

# 3. 映射到全局变量 (这样你下面的业务代码一行都不用改！)
SOURCE_DIR = CFG["SOURCE_DIR"]
DB_FILE = CFG["DB_FILE"]
MAX_PROCESSES = CFG["MAX_PROCESSES"]
AUDIO_THRESHOLD = CFG["AUDIO_THRESHOLD"]
VISUAL_COVERAGE_THRESHOLD = CFG["VISUAL_COVERAGE"]
HAMMING_TOLERANCE = CFG["HAMMING_TOLERANCE"]
SAFE_DURATION_DIFF = CFG["SAFE_DURATION_DIFF"]
# ===============================================




def find_tool(name):
    import shutil
    path = shutil.which(name)
    if not path and os.path.exists(name + ".exe"): return os.path.abspath(name + ".exe")
    return path

FPCALC_PATH = find_tool("fpcalc")
FFPROBE_PATH = find_tool("ffprobe")
# ===========================================

def init_db():
    """初始化数据库结构"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # 主表：status: 0=新, 1=待洗, 2=疑似, 3=保留(父), 99=待删(子), 100=已移, -1=错
    c.execute('''CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT UNIQUE,
                    size_bytes INTEGER,
                    duration REAL,
                    width INTEGER,
                    height INTEGER,
                    bitrate INTEGER,
                    codec TEXT,
                    status INTEGER DEFAULT 0,
                    similarity_info TEXT
                )''')

    # 音频指纹表 (BLOB存储)
    c.execute('''CREATE TABLE IF NOT EXISTS audio_fingerprints (
                    video_id INTEGER PRIMARY KEY,
                    fingerprint BLOB,
                    FOREIGN KEY(video_id) REFERENCES videos(id)
                )''')

    # 视觉指纹表 (密集采样)
    c.execute('''CREATE TABLE IF NOT EXISTS visual_hashes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER,
                    phash TEXT,
                    FOREIGN KEY(video_id) REFERENCES videos(id)
                )''')
    
    # 索引优化
    c.execute('CREATE INDEX IF NOT EXISTS idx_videos_path ON videos (path)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_videos_status ON videos (status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_visual_phash ON visual_hashes (phash)')
    
    conn.commit()
    conn.close()

def prune_deleted_files(conn):
    """【同步】清理数据库中硬盘已不存在的文件记录"""
    print(" 检查幽灵文件...")
    c = conn.cursor()
    c.execute("SELECT id, path FROM videos")
    rows = c.fetchall()
    
    deleted_ids = []
    for vid, path in rows:
        if not os.path.exists(path):
            deleted_ids.append(vid)
    
    if deleted_ids:
        print(f"   发现 {len(deleted_ids)} 个文件已删除，正在清理数据库...")
        for vid in deleted_ids:
            c.execute("DELETE FROM audio_fingerprints WHERE video_id=?", (vid,))
            c.execute("DELETE FROM visual_hashes WHERE video_id=?", (vid,))
            c.execute("DELETE FROM videos WHERE id=?", (vid,))
        conn.commit()
        print("    清理完成。")
    else:
        print("    数据库与硬盘同步。")

def demote_lonely_parents(conn):
    """【同步】如果父节点(Status=3)的所有子节点都已删除，将其降级为普通(Status=1)"""
    print(" 检查光杆司令...")
    c = conn.cursor()
    c.execute("SELECT id, path FROM videos WHERE status = 3")
    parents = c.fetchall()
    
    demoted_count = 0
    for pid, path in parents:
        # 查询是否有子节点指向它 (status 99或100, 且info包含 ID_xxx)
        pattern = f"%ID_{pid}%"
        c.execute("SELECT count(*) FROM videos WHERE (status=99 OR status=100) AND similarity_info LIKE ?", (pattern,))
        if c.fetchone()[0] == 0:
            c.execute("UPDATE videos SET status=1, similarity_info=NULL WHERE id=?", (pid,))
            demoted_count += 1

    if demoted_count > 0:
        conn.commit()
        print(f"    已重置 {demoted_count} 个无子节点的父文件，它们将重新参与比对。")
    else:
        print("    家族关系正常。")

def get_metadata_and_fingerprint(file_path):
    """工作进程：提取元数据和音频指纹"""
    meta = {'duration': 0.0, 'width': 0, 'height': 0, 'bitrate': 0, 'codec': 'unknown'}
    
    # 1. ffprobe 提取元数据
    try:
        cmd_probe = [FFPROBE_PATH, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", file_path]
        # Win隐藏窗口
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
        out = subprocess.check_output(cmd_probe, startupinfo=startupinfo)
        data = json.loads(out)
        
        if 'format' in data:
            meta['duration'] = float(data['format'].get('duration', 0))
            meta['bitrate'] = int(data['format'].get('bit_rate', 0))
            if meta['bitrate'] == 0 and meta['duration'] > 0: # 估算码率
                meta['bitrate'] = int((int(data['format'].get('size', 0)) * 8) / meta['duration'])

        for s in data.get('streams', []):
            if s.get('codec_type') == 'video':
                meta['width'] = int(s.get('width', 0))
                meta['height'] = int(s.get('height', 0))
                meta['codec'] = s.get('codec_name', 'unknown')
                break
    except Exception as e:
        return (False, file_path, None, None, f"ffprobe: {e}")

    # 2. fpcalc 提取音频指纹
    try:
        cmd_fp = [FPCALC_PATH, "-raw", "-json", "-length", "0", file_path] # -length 0 读全片
        proc = subprocess.Popen(cmd_fp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
        stdout, _ = proc.communicate()
        
        if proc.returncode != 0:
            return (True, file_path, meta, None, "No audio/fpcalc fail") # 无音频不算致命错
            
        fp_data = json.loads(stdout)
        fingerprint = fp_data.get('fingerprint', [])
        if meta['duration'] == 0: meta['duration'] = fp_data.get('duration', 0)
            
        return (True, file_path, meta, fingerprint, None)
    except Exception as e:
        return (False, file_path, meta, None, f"fpcalc: {e}")

def scan_and_register_files(conn):
    """增量扫描新文件入库"""
    print("[Scan] 扫描新文件...")
    video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.ts')
    
    c = conn.cursor()
    c.execute("SELECT path FROM videos")
    existing_paths = set(row[0] for row in c.fetchall())
    
    new_files = []
    for root, _, files in os.walk(SOURCE_DIR):
        if "_Duplicates" in root or "_Manual" in root: continue # 跳过特殊目录
        for f in files:
            if f.lower().endswith(video_exts):
                full_path = os.path.join(root, f)
                if full_path not in existing_paths:
                    new_files.append((full_path, os.path.getsize(full_path)))
    
    if new_files:
        print(f"[Found] 发现 {len(new_files)} 个新文件，正在注册...")
        c.executemany("INSERT INTO videos (path, size_bytes, status) VALUES (?, ?, 0)", new_files)
        conn.commit()
    else:
        print(" 无新文件。")

def main():
    if not FPCALC_PATH or not FFPROBE_PATH:
        print("[Loss] 缺少工具：请确保 fpcalc.exe 和 ffprobe.exe 在同级目录。")
        return

    init_db()
    conn = sqlite3.connect(DB_FILE)
    
    # 步骤 A: 维护数据库一致性
    prune_deleted_files(conn)    # 删掉不在硬盘的
    demote_lonely_parents(conn)  # 重置没儿子的爹
    scan_and_register_files(conn)# 添加新文件
    
    # 步骤 B: 提取指纹 (针对 status=0 的新文件)
    c = conn.cursor()
    c.execute("SELECT id, path FROM videos WHERE status = 0")
    tasks = c.fetchall()
    
    if not tasks:
        print(" 所有文件均已就绪 (Audio Ready)。")
        conn.close()
        return

    print(f" 开始处理 {len(tasks)} 个新视频...")
    file_list = [t[1] for t in tasks]
    path_to_id = {t[1]: t[0] for t in tasks}
    
    # 多进程并行处理
    hdd_processes = 1
    #with Pool(processes=max(1, cpu_count() - 1)) as pool:
    with Pool(processes=hdd_processes) as pool:
        results_iter = pool.imap_unordered(get_metadata_and_fingerprint, file_list)
        
        for res in tqdm(results_iter, total=len(file_list)):
            success, path, meta, fp, err = res
            vid = path_to_id[path]
            
            if success:
                # 成功: status -> 1
                c.execute("""UPDATE videos SET duration=?, width=?, height=?, bitrate=?, codec=?, status=1 
                             WHERE id=?""", (meta['duration'], meta['width'], meta['height'], meta['bitrate'], meta['codec'], vid))
                if fp:
                    c.execute("INSERT OR REPLACE INTO audio_fingerprints (video_id, fingerprint) VALUES (?, ?)", (vid, pickle.dumps(fp)))
            else:
                # 失败: status -> -1
                print(f"\n️ {os.path.basename(path)} 失败: {err}")
                c.execute("UPDATE videos SET status=-1 WHERE id=?", (vid,))
            
            if vid % 20 == 0: conn.commit() # 定期提交

    conn.commit()
    conn.close()
    print("\n[Done] 完成。请运行下一步。")

if __name__ == '__main__':
    freeze_support()
    main()