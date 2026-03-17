import sqlite3
import os
import sys
import json
import subprocess
import re
import random
import difflib
import numpy as np
from tqdm import tqdm

# ================= 动态配置加载 =================
CFG = {
    "DB_FILE": "video_library.db",
    "ASR_MODEL": "small",          # 留作占位，内部已硬编码使用阿里 Paraformer
    "ASR_EXPORT_DIR": "asr_texts", # config中定义的导出目录
    "ASR_BATCH_SIZE": 50,          # 50个一组 
    "ASR_TEXT_THRESHOLD": 0.6,     # 视频间60%句子重合判定为重复
    "SENTENCE_SIMILARITY": 0.65,   # 句子相似度(使用difflib，0.65即可完美兼容少字漏字)
    "SAFE_DURATION_DIFF": 30
}

# 接收主控 GUI 传过来的 config.json 并更新配置
if len(sys.argv) > 1 and sys.argv[1].endswith('.json') and os.path.exists(sys.argv[1]):
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        CFG.update(json.load(f))

# 映射给全局变量
DB_FILE = os.path.abspath(CFG["DB_FILE"])
#WORKSPACE_DIR = os.path.dirname(DB_FILE)
#EXPORT_DIR = os.path.join(WORKSPACE_DIR, CFG["ASR_EXPORT_DIR"])
EXPORT_DIR = os.path.abspath(CFG["ASR_EXPORT_DIR"])


TEXT_THRESHOLD = CFG["ASR_TEXT_THRESHOLD"]
SENT_SIM_LIMIT = CFG["SENTENCE_SIMILARITY"]
SAFE_DURATION_DIFF = CFG["SAFE_DURATION_DIFF"]

# ================= 数据库初始化 =================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS text_segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER,
                    start_time REAL,
                    content TEXT,
                    FOREIGN KEY(video_id) REFERENCES videos(id)
                )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_text_vid ON text_segments (video_id)')
    conn.commit()
    return conn

# ================= 文本工具 =================
def split_to_sentences(raw_text):
    """针对阿里输出的一整段文本，进行多重标点断句"""
    parts = re.split(r'[，。！？；\s,.!?;…]+', raw_text)
    return [p.strip() for p in parts if len(p.strip()) >= 2]

def clean_text_for_match(text):
    """🔥 核心洗稿处理：去标点、去特殊语气词、全小写 (与 GUI 完全同步)"""
    t = re.sub(r'[^\w\u4e00-\u9fa5]', '', text)
    t = re.sub(r'[嗯啊哦哎呀呢啦哈呗嘛]', '', t)
    return t.strip().lower()

def get_text_sim(str1, str2):
    """
    🔥 使用内置的 SequenceMatcher 计算相似度
    能够完美应对“你拿手机干啥呀”和“我奔你手机干啥呀”的对比
    """
    if not str1 or not str2: return 0
    return difflib.SequenceMatcher(None, str1, str2).ratio()

# ================= 阿里 FunASR 提取逻辑 =================
def extract_audio_ffmpeg(video_path, temp_wav):
    """使用 ffmpeg 极速提取 16kHz 单声道 wav 供阿里模型使用"""
    cmd =[
        'ffmpeg', '-y', '-i', video_path, 
        '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', 
        temp_wav
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception as e:
        print(f"️ 音频提取失败[{os.path.basename(video_path)}]: {e}")
        return False

# ================= 阿里 FunASR 提取逻辑 (已升级为极速 ONNX 版) =================
def run_native_asr_extraction(conn, video_tasks):
    """使用极速本地 ONNX 模型进行批量提取，告别 5GB 显卡包袱"""
    if not video_tasks:
        return
        
    try:
        from funasr_onnx import Fsmn_vad, Paraformer, CT_Transformer
        import soundfile as sf
    except ImportError as e:
        print(" 未检测到 funasr-onnx 或 soundfile，请检查环境。")
        print(f" 具体的错误提示为: {e}")  # 这里会打印出真实的错误原因
        sys.exit(1)

    if not os.path.exists(EXPORT_DIR): os.makedirs(EXPORT_DIR)
    json_keep_dir = os.path.join(EXPORT_DIR, "json_raw")
    if not os.path.exists(json_keep_dir): os.makedirs(json_keep_dir)
    
    # 临时音频文件路径
    temp_wav = os.path.join(EXPORT_DIR, "_temp_audio.wav")
    temp_chunk_path = os.path.join(EXPORT_DIR, "_temp_chunk.wav")

    # 🔥 获取本地模型路径 (兼容开发与打包环境)
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    vad_model_dir = os.path.join(base_dir, "models", "speech_fsmn_vad_zh-cn-16k-common-pytorch")
    asr_model_dir = os.path.join(base_dir, "models", "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch")
    punc_model_dir = os.path.join(base_dir, "models", "punc_ct-transformer_zh-cn-common-vocab272727-pytorch")

    print(f"\n 正在加载极速本地 ONNX 模型 (仅需几百兆内存)...")
    try:
        vad_model = Fsmn_vad(vad_model_dir, quantize=True)
        asr_model = Paraformer(asr_model_dir, batch_size=1, quantize=True)
        punc_model = CT_Transformer(punc_model_dir, quantize=True)
    except Exception as e:
        print(f" 模型加载失败，请检查 models 文件夹及 ONNX 模型是否完整: {e}")
        sys.exit(1)

    print(f" 模型加载完毕！开始处理 {len(video_tasks)} 个视频...\n")
    
    # 暴力提取文字函数 (应对官方奇怪的返回格式)
    def extract_string(obj):
        if isinstance(obj, str): return obj
        if isinstance(obj, dict):
            val = obj.get("text", "") or obj.get("preds", "")
            if val: return extract_string(val)
            for v in obj.values():
                res = extract_string(v)
                if res: return res
            return ""
        if isinstance(obj, (list, tuple)):
            for item in obj:
                res = extract_string(item)
                if res: return res
            return ""
        return ""

    c = conn.cursor()
    success_count = 0
    total_segments_saved = 0

    for vid, path in tqdm(video_tasks, desc="ASR 转录进度"):
        base_name = os.path.basename(path)
        
        # 1. 提取单声道 wav
        if not extract_audio_ffmpeg(path, temp_wav):
            continue
            
        try:
            # 2. VAD 切片
            vad_segments = vad_model([temp_wav])
            if not vad_segments or not vad_segments[0]: 
                continue

            speech, sample_rate = sf.read(temp_wav)
            full_text_list =[]

            # 3. 逐段识别并加标点
            for segment in vad_segments[0]:
                start_ms, end_ms = segment[0], segment[1]
                start_idx = int(start_ms * sample_rate / 1000)
                end_idx = int(end_ms * sample_rate / 1000)
                audio_chunk = speech[start_idx:end_idx]

                sf.write(temp_chunk_path, audio_chunk, sample_rate)

                asr_res = asr_model([temp_chunk_path])
                raw_text = extract_string(asr_res).strip()

                if raw_text:
                    punc_res = punc_model(raw_text)
                    punctuated_text = extract_string(punc_res) or raw_text
                    full_text_list.append(punctuated_text)

            full_text = "".join(full_text_list)
            if not full_text.strip(): 
                continue
                
            # 🔥 伪装成原版的 res 结构，以便向下兼容保存 json_raw
            res = [{"text": full_text}]
            json_path = os.path.join(json_keep_dir, f"{base_name}.json")
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(res, f, ensure_ascii=False, indent=4)
            
            db_batch = []
            readable_lines =[]
            
            # 使用现有的正则基于标点进行物理断句
            short_sentences = split_to_sentences(full_text)
            
            for idx, sub in enumerate(short_sentences):
                # 防复读兜底
                if readable_lines and sub in readable_lines[-1]: continue
                    
                db_batch.append((vid, float(idx), sub))
                readable_lines.append(f"[句子 {idx+1:03d}] {sub}")
            
            if db_batch:
                c.executemany("INSERT INTO text_segments (video_id, start_time, content) VALUES (?, ?, ?)", db_batch)
                total_segments_saved += len(db_batch)
                success_count += 1
                
            txt_path = os.path.join(EXPORT_DIR, f"{base_name}.asr.txt")
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(readable_lines))
                
        except Exception as e:
            tqdm.write(f"❌ 处理 {base_name} 时发生异常: {e}")
            
    # 清理所有的临时占位音频文件
    for t_file in [temp_wav, temp_chunk_path]:
        if os.path.exists(t_file):
            try: os.remove(t_file)
            except: pass
        
    conn.commit()
    print(f"\n ASR 提取阶段完成！成功处理 {success_count} 个视频，录入 {total_segments_saved} 句纯净台词。")

# ================= 🚀 终极光速版：ASR 匹配算法 =================
def run_matching(conn):
    c = conn.cursor()
    print("\n 正在加载台词库进行特征向量化...")
    
    c.execute("SELECT id, path, duration, size_bytes FROM videos WHERE status = 1")
    videos = {r[0]: {'id': r[0], 'path': r[1], 'dur': r[2], 'size': r[3], 'sentences':[]} for r in c.fetchall()}
    
    c.execute("SELECT video_id, content FROM text_segments")
    for vid, content in c.fetchall():
        if vid in videos:
            videos[vid]['sentences'].append(content)
            
    data_list = [v for v in videos.values() if len(v['sentences']) > 1]
    data_list.sort(key=lambda x: (x['dur'], x['size']), reverse=True)
    
    if not data_list:
        print(" 暂无足够的台词数据进行比对。")
        return

    # 🚀 预处理阶段：缓存所有长度，生成哈希集合字典，杜绝在循环中重复计算
    for v in data_list:
        clean_items =[]
        all_ngrams = set()
        text_set = set()  # 🌟 O(1) 精确匹配字典库
        
        for s in v['sentences']:
            cln = clean_text_for_match(s)
            length = len(cln)
            if length >= 2:
                ngrams = set(cln[k:k+2] for k in range(length-1))
                if not ngrams: ngrams = set(list(cln))
                
                clean_items.append({
                    'text': cln, 
                    'len': length,
                    'ngrams': ngrams,
                    'ngram_len': len(ngrams)
                })
                all_ngrams.update(ngrams)
                text_set.add(cln)
                
        v['clean_sentences'] = clean_items
        v['all_ngrams'] = all_ngrams
        v['text_set'] = text_set  # 注入哈希字典

    processed_ids = set()
    total_matches = 0

    print(" 启动 【O(1)哈希 + 数学熔断】 光速比对引擎...")
    
    for i in tqdm(range(len(data_list)), desc="Matching Scripts"):
        parent = data_list[i]
        if parent['id'] in processed_ids: continue
        if not parent['clean_sentences']: continue
        
        parent_texts = parent['text_set']  # 取出当前基准视频的哈希字典
        
        for j in range(i + 1, len(data_list)):
            child = data_list[j]
            if child['id'] in processed_ids: continue
            if child['dur'] > parent['dur'] + SAFE_DURATION_DIFF: continue
            if not child['clean_sentences']: continue
            
            # =========================================================
            # 🔪 第一刀：宏观词组防碰瓷 (阈值提升至 25%)
            # =========================================================
            if not child['all_ngrams'] or not parent['all_ngrams']: continue
            video_overlap = len(child['all_ngrams'] & parent['all_ngrams']) / len(child['all_ngrams'])
            if video_overlap < 0.24:
                continue
            
            # =========================================================
            # 🛡️ 核心装备：数学熔断器 (Branch and Bound 算法)
            # =========================================================
            total_cs = len(child['clean_sentences'])
            required_matches = total_cs * TEXT_THRESHOLD
            max_allowed_misses = total_cs - required_matches
            
            matches = 0
            misses = 0
            
            for cs in child['clean_sentences']:
                
                # =========================================================
                # 🚀 第二刀：O(1) 哈希秒杀！(如果是原话，0.0001秒判定)
                # =========================================================
                if cs['text'] in parent_texts:
                    matches += 1
                    continue
                
                # 开始模糊查找
                best_score = 0
                for ps in parent['clean_sentences']:
                    if abs(cs['len'] - ps['len']) > 15: continue 
                    
                    min_len = min(cs['ngram_len'], ps['ngram_len'])
                    if min_len > 0:
                        # 句子微观防碰瓷
                        if len(cs['ngrams'] & ps['ngrams']) / min_len < 0.3:
                            continue
                    
                    # 最耗时的操作，只有真正高度疑似的句子才会被送到这里
                    score = difflib.SequenceMatcher(None, cs['text'], ps['text']).ratio()
                    if score > best_score:
                        best_score = score
                        if best_score >= SENT_SIM_LIMIT: 
                            break
                            
                if best_score >= SENT_SIM_LIMIT: 
                    matches += 1
                else:
                    misses += 1
                    
                    # =========================================================
                    # 💥 第三刀：无情熔断！
                    # 如果失败次数已经超标，无论如何都不可能达标了，直接掐断循环！
                    # =========================================================
                    if misses > max_allowed_misses:
                        break
            
            # 结算
            coverage = matches / total_cs if total_cs > 0 else 0
            
            if coverage >= TEXT_THRESHOLD:
                total_matches += 1
                processed_ids.add(child['id'])
                info = f"ASR匹配 ID_{parent['id']} | 重合率:{coverage:.1%}"
                c.execute("UPDATE videos SET status=99, similarity_info=? WHERE id=?", (info, child['id']))
                c.execute("UPDATE videos SET status=3, similarity_info='ASR基准:保留' WHERE id=?", (parent['id'],))
        
        if i % 10 == 0: conn.commit()
    conn.commit()
    print(f"\n 比对完成，共发现 {total_matches} 组重复内容。")

# ================= 主控制流 =================
def main():
    conn = init_db()
    
    c = conn.cursor()
    c.execute('''SELECT id, path FROM videos 
                 WHERE status = 1 AND id NOT IN (SELECT DISTINCT video_id FROM text_segments)''')
    all_pending = c.fetchall()
    
    if all_pending:
        random.shuffle(all_pending)
        print(f" 发现 {len(all_pending)} 个待转写视频 (已启用阿里 FunASR 工业流水线)。")
        run_native_asr_extraction(conn, all_pending)
    else:
        print(" 所有视频均已完成 ASR 特征提取。")

    run_matching(conn)
    conn.close()

if __name__ == '__main__':
    main()