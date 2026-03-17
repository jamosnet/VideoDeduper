import sqlite3
import cv2
import imagehash
from PIL import Image
import os
import sys
import json
import numpy as np
from multiprocessing import Pool, cpu_count, freeze_support
from tqdm import tqdm

# ================= 动态配置加载 =================
CFG = {
    "DB_FILE": "video_library.db",
    "MAX_PROCESSES": 3,
    "SAMPLE_INTERVAL": 3,
    "VISUAL_COVERAGE": 0.6,
    "HAMMING_TOLERANCE": 9,
    "SAFE_DURATION_DIFF": 30
}

# 接收主控 GUI 传过来的 config.json
if len(sys.argv) > 1 and sys.argv[1].endswith('.json') and os.path.exists(sys.argv[1]):
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        CFG.update(json.load(f))

# 映射给全局变量，下面的业务代码就不用大改了
DB_FILE = CFG["DB_FILE"]
MAX_PROCESSES = CFG.get("MAX_PROCESSES", 3)
SAMPLE_INTERVAL = CFG.get("SAMPLE_INTERVAL", 3)
VISUAL_COVERAGE_THRESHOLD = CFG["VISUAL_COVERAGE"]
HAMMING_TOLERANCE = CFG["HAMMING_TOLERANCE"]
SAFE_DURATION_DIFF = CFG["SAFE_DURATION_DIFF"]
# ===============================================




def get_dense_visual_hashes(file_path):
    """工作进程: 高密度提取视觉特征"""
    hashes =[]
    try:
        cap = cv2.VideoCapture(file_path)
        if not cap.isOpened(): return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        frame_step = max(1, int(fps * SAMPLE_INTERVAL))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        curr_frame = 0
        while curr_frame < total_frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, curr_frame)
            ret, frame = cap.read()
            if ret:
                try:
                    img = Image.fromarray(cv2.cvtColor(cv2.resize(frame, (128, 128)), cv2.COLOR_BGR2RGB))
                    hashes.append(str(imagehash.phash(img))) # 存为16进制字符串
                except: pass
            curr_frame += frame_step
        cap.release()
        return hashes
    except: return[]

def load_or_build_index(conn):
    """检查并增量生成视觉指纹库"""
    c = conn.cursor()
    print(" 检查视觉特征索引...")
    c.execute("SELECT id, path FROM videos WHERE status = 1")
    candidates = c.fetchall()
    
    c.execute("SELECT DISTINCT video_id FROM visual_hashes")
    existing_ids = set(r[0] for r in c.fetchall())
    tasks = [r for r in candidates if r[0] not in existing_ids]
    
    if not tasks:
        print(" 视觉特征库已就绪。")
        return

    print(f" 需要为 {len(tasks)} 个视频生成视觉指纹 (多进程加速中)...")
    task_args = [t[1] for t in tasks]
    task_ids = [t[0] for t in tasks]
    
    with Pool(processes=MAX_PROCESSES) as pool:
        results = list(tqdm(pool.imap(get_dense_visual_hashes, task_args), total=len(tasks)))
        print(" 写入特征库...")
        batch =[]
        for i, h_list in enumerate(results):
            if h_list:
                batch.extend([(task_ids[i], h) for h in h_list])
            if len(batch) > 50000:
                c.executemany("INSERT INTO visual_hashes (video_id, phash) VALUES (?, ?)", batch)
                conn.commit()
                batch =[]
        if batch:
            c.executemany("INSERT INTO visual_hashes (video_id, phash) VALUES (?, ?)", batch)
            conn.commit()

def load_data_to_memory(conn):
    """加载指纹并转换为 NumPy uint64 数组以供极速矩阵运算"""
    print(" 正在加载矩阵数据到内存...")
    c = conn.cursor()
    c.execute("SELECT id, path, size_bytes, duration, width, height FROM videos WHERE status = 1")
    videos = {r[0]: {'id': r[0], 'path': r[1], 'size': r[2], 'dur': r[3], 'res': f"{r[4]}x{r[5]}", 'hashes':[]} for r in c.fetchall()}
    
    c.execute("SELECT video_id, phash FROM visual_hashes")
    for vid, phash_str in c.fetchall():
        if vid in videos: videos[vid]['hashes'].append(int(phash_str, 16))
            
    valid_list = []
    for v in videos.values():
        if len(v['hashes']) > 3:
            # 转为一维 uint64 数组并去重
            v['hash_arr'] = np.unique(np.array(v['hashes'], dtype=np.uint64)) 
            valid_list.append(v)
            
    print(f" 已加载 {len(valid_list)} 个有效视频矩阵。")
    return valid_list

def calc_coverage_fast(parent_arr, child_arr):
    """
    🔥 NumPy 矩阵广播加速算法 (已修复维度越界 Bug)
    """
    if child_arr.size == 0 or parent_arr.size == 0: return 0.0
    
    # 改变维度以触发矩阵广播: child(N, 1) XOR parent(1, M) -> 结果矩阵(N, M)
    xor_matrix = np.bitwise_xor(child_arr[:, None], parent_arr[None, :])
    
    # 🔥 修复 Bug: 强制增加一个尾部维度 (N, M, 1)，再 view 成 (N, M, 8) 的字节矩阵
    xor_uint8 = np.ascontiguousarray(xor_matrix)[..., None].view(np.uint8) 
    
    # 将 8 个字节拆成 64 个位，形状变为 (N, M, 64)
    bits = np.unpackbits(xor_uint8, axis=2)                     
    # 累加这 64 个位，得到实际的汉明距离矩阵 (N, M)
    distances = bits.sum(axis=2)                                
    
    # 检查 Child 中的每个 Hash，是否在 Parent 中有距离 <= 容差的匹配
    hits = np.any(distances <= HAMMING_TOLERANCE, axis=1)       # 布尔数组 (N,)
    return np.sum(hits) / child_arr.size                        # 返回覆盖率

def main():
    conn = sqlite3.connect(DB_FILE)
    load_or_build_index(conn)
    data_list = load_data_to_memory(conn)
    if not data_list: return

    # 时长降序 -> 体积降序 (老大在前)
    data_list.sort(key=lambda x: (x['dur'], x['size']), reverse=True)
    processed_ids = set()
    
    match_groups = 0
    total_moved = 0
    
    print(f"\n 开始视觉倒排搜索[NumPy 矩阵加速版] (容差: {HAMMING_TOLERANCE}, 阈值: {VISUAL_COVERAGE_THRESHOLD})...\n")
    
    for i in tqdm(range(len(data_list)), desc="Visual Searching"):
        parent = data_list[i]
        if parent['id'] in processed_ids: continue
        
        family = []
        updates =[] 
        
        for j in range(i + 1, len(data_list)):
            child = data_list[j]
            if child['id'] in processed_ids: continue
            if child['dur'] > parent['dur'] + SAFE_DURATION_DIFF: continue # 时长保护
                
            # 极速矩阵计算覆盖率
            ratio = calc_coverage_fast(parent['hash_arr'], child['hash_arr'])
            
            if ratio >= VISUAL_COVERAGE_THRESHOLD:
                is_full = abs(child['dur'] - parent['dur']) < 15
                rtype = "视觉:变速/变调" if is_full else "视觉:剪辑片段"
                family.append({'data': child, 'ratio': ratio, 'type': rtype})
                processed_ids.add(child['id'])
        
        # 实时处理并存盘
        if family:
            match_groups += 1
            tqdm.write(f" [基准] {os.path.basename(parent['path'])} | {int(parent['dur'])}s | {parent['res']}")
            updates.append((3, "视觉基准:保留", parent['id']))
            
            for m in family:
                c, r, t = m['data'], m['ratio'], m['type']
                updates.append((99, f"匹配 ID_{parent['id']} | 覆盖率: {r:.1%} | {t}", c['id']))
                total_moved += 1
                tqdm.write(f"   └──  [移动] {os.path.basename(c['path'])} | {int(c['dur'])}s | 覆盖率: {r:.1%}")
            tqdm.write("")
            
            conn.executemany("UPDATE videos SET status=?, similarity_info=? WHERE id=?", updates)
            conn.commit()

    conn.close()
    print("-" * 50)
    print(f" 搜索完成! 发现 {match_groups} 组, 标记 {total_moved} 个待删文件。")

if __name__ == '__main__':
    freeze_support()
    main()