import sqlite3
import pickle
import os
from tqdm import tqdm

# ================= 配置 =================
#DB_FILE = "video_library.db"

# 音频重合度阈值 (0.5 表示 B 的特征有一半以上在 A 里)
# 对于剪辑版，只要声音没变调，这个值通常很高 (0.8+)
#AUDIO_THRESHOLD = 0.5 

# 安全锁：如果 被删文件 比 基准文件 长出这么多秒，则不删 (防止误判)
#SAFE_DURATION_DIFF = 30 
# =======================================


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
            print(f"️ 无法读取工作区配置: {e}")

# 3. 映射到全局变量 (这样你下面的业务代码一行都不用改！)
SOURCE_DIR = CFG["SOURCE_DIR"]
DB_FILE = CFG["DB_FILE"]
MAX_PROCESSES = CFG["MAX_PROCESSES"]
AUDIO_THRESHOLD = CFG["AUDIO_THRESHOLD"]
VISUAL_COVERAGE_THRESHOLD = CFG["VISUAL_COVERAGE"]
HAMMING_TOLERANCE = CFG["HAMMING_TOLERANCE"]
SAFE_DURATION_DIFF = CFG["SAFE_DURATION_DIFF"]
# ===============================================




def load_data(conn):
    """加载所有已提取指纹的视频数据"""
    print(" 正在加载音频指纹数据到内存...")
    c = conn.cursor()
    # 联表查询：元数据 + 指纹
    c.execute('''
        SELECT v.id, v.path, v.size_bytes, v.duration, v.width, v.height, a.fingerprint 
        FROM videos v
        JOIN audio_fingerprints a ON v.id = a.video_id
        WHERE v.status = 1
    ''')
    rows = c.fetchall()
    
    data = []
    for r in rows:
        vid, path, size, dur, w, h, fp_blob = r
        if fp_blob:
            fp = pickle.loads(fp_blob)
            # 预计算 set 以加速后续对比
            data.append({
                'id': vid,
                'path': path,
                'size': size,
                'dur': dur,
                'res': f"{w}x{h}",
                'fp_set': set(fp), 
                'fp_len': len(fp) # 原始长度，用于辅助判断
            })
    print(f" 已加载 {len(data)} 个有效音频指纹。")
    return data

def format_size(size):
    return f"{size / 1024 / 1024:.1f}MB"

def main():
    conn = sqlite3.connect(DB_FILE)
    
    # 1. 加载数据
    data_list = load_data(conn)
    if not data_list:
        print("没有待处理的视频 (Status=1)。请先运行 db_builder。")
        return

    # 2. 排序：按【时长降序】 -> 【体积降序】
    # 策略：优先认为“长视频”是“短视频”的父亲。
    # 如果时长一样，体积大的是父亲。
    data_list.sort(key=lambda x: (x['dur'], x['size']), reverse=True)

    processed_ids = set()
    updates = [] # 待执行的数据库更新 [(status, info, id)]
    
    match_groups = 0
    total_moved = 0

    print(f"\n 开始全量音频拓扑分析 (阈值: {AUDIO_THRESHOLD})...\n")

    # 3. 循环对比
    # 这是一个 N*N 的过程，使用进度条
    for i in tqdm(range(len(data_list)), desc="Analyzing"):
        parent = data_list[i]
        
        # 如果这个视频已经被认领为儿子了，就跳过，不再当爹
        if parent['id'] in processed_ids:
            continue
            
        # 创建家族组
        family = [] 
        
        for j in range(i + 1, len(data_list)):
            child = data_list[j]
            
            if child['id'] in processed_ids:
                continue
            
            # --- 核心算法：集合包含 (Subset Check) ---
            # 检查 child 是否是 parent 的子集
            # 交集 / Child的长度
            intersection = len(parent['fp_set'] & child['fp_set'])
            child_len = len(child['fp_set'])
            
            if child_len == 0: continue
            
            ratio = intersection / child_len
            
            if ratio >= AUDIO_THRESHOLD:
                # 命中！
                
                # 再次检查：时长保护
                # 虽然按时长排过序，但 fpcalc 的 duration 有时有微小误差，或者指纹错判
                # 如果 Child 竟然比 Parent 明显长，那是异常情况，不能删
                if child['dur'] > parent['dur'] + SAFE_DURATION_DIFF:
                    continue

                # 记录关系
                relation_type = "完全重复" if abs(child['dur'] - parent['dur']) < 10 else "剪辑片段"
                family.append({
                    'data': child,
                    'ratio': ratio,
                    'type': relation_type
                })
                
                # 标记 Child 已处理
                processed_ids.add(child['id'])

        # 4. 处理家族结果
        if family:
            match_groups += 1
            
            # 打印家族树 (满足你的列出要求)
            tqdm.write(f"⭐ [家族基准] {os.path.basename(parent['path'])}")
            tqdm.write(f"   ℹ️  {int(parent['dur'])}s | {parent['res']} | {format_size(parent['size'])}")
            
            # 只有当家族里有东西时，Parent 才标记为 3 (保留的主节点)
            # 否则 Parent 保持 1，留给下一轮视觉搜索
            updates.append((3, "音频基准:保留", parent['id']))
            
            for member in family:
                child = member['data']
                ratio = member['ratio']
                rtype = member['type']
                
                # 构造详细的相似信息
                sim_info = f"匹配: ID_{parent['id']} | 相似度: {ratio:.1%} | 类型: {rtype}"
                
                # 标记为 99 (待删除)
                updates.append((99, sim_info, child['id']))
                total_moved += 1
                
                tqdm.write(f"   └── ❌ [移动] {os.path.basename(child['path'])}")
                tqdm.write(f"       └─ {int(child['dur'])}s | {child['res']} | {format_size(child['size'])} | 相似度: {ratio:.1%} ({rtype})")
            
            tqdm.write("") # 空行分隔

    # 5. 批量更新数据库
    print(f"\n 正在更新数据库状态 ({len(updates)} 条记录)...")
    c = conn.cursor()
    c.executemany("UPDATE videos SET status=?, similarity_info=? WHERE id=?", updates)
    conn.commit()
    conn.close()

    print("-" * 50)
    print(f" 统计报告:")
    print(f"   发现 {match_groups} 个相似组")
    print(f"   标记 {total_moved} 个文件为 [待移动/删除]")
    print(f"   剩余未匹配文件将保留 status=1，进入下一步视觉搜索。")
    print("-" * 50)
    print(" 提示：被标记为 99 的文件现在还在原处。")
    print("   你需要运行一个简单的移动脚本来实际执行移动操作，或者手动检查数据库。")

if __name__ == '__main__':
    main()