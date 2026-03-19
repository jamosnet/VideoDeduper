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
from codetiming import Timer

# ================= 动态配置加载 =================
CFG = {
    "DB_FILE": "video_library.db",
    "ASR_MODEL": "small",  # 留作占位，内部已硬编码使用阿里 Paraformer
    "ASR_EXPORT_DIR": "asr_texts",  # config中定义的导出目录
    "ASR_BATCH_SIZE": 50,  # 50个一组
    "ASR_TEXT_THRESHOLD": 0.6,  # 视频间60%句子重合判定为重复
    "SENTENCE_SIMILARITY": 0.65,  # 句子相似度(使用difflib，0.65即可完美兼容少字漏字)
    "SAFE_DURATION_DIFF": 30
}

# 接收主控 GUI 传过来的 config.json 并更新配置
if len(sys.argv) > 1 and sys.argv[1].endswith('.json') and os.path.exists(sys.argv[1]):
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        CFG.update(json.load(f))

# 映射给全局变量
DB_FILE = os.path.abspath(CFG["DB_FILE"])
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


# ================= 阿里 FunASR 提取逻辑 =================
def extract_audio_ffmpeg(video_path, temp_wav):
    """使用 ffmpeg 极速提取 16kHz 单声道 wav 供阿里模型使用"""
    cmd = [
        'ffmpeg', '-y', '-i', video_path,
        '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        temp_wav
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception as e:
        print(f"[Err] 音频提取失败[{os.path.basename(video_path)}]: {e}")
        return False


# ================= 阿里 FunASR 提取逻辑 (GPU 优化版) =================
def run_native_asr_extraction(conn, video_tasks):
    """使用 Python 原生常驻阿里模型进行提取，支持 GPU 加速"""
    if not video_tasks:
        return

    try:
        import torch
        from funasr import AutoModel
    except ImportError as e:
        print("[x] 未检测到 funasr 或 torch，请检查环境。")
        print(f"[info] 具体的错误提示为: {e}")
        sys.exit(1)

    if not os.path.exists(EXPORT_DIR): os.makedirs(EXPORT_DIR)
    json_keep_dir = os.path.join(EXPORT_DIR, "json_raw")
    if not os.path.exists(json_keep_dir): os.makedirs(json_keep_dir)

    # 临时音频文件路径
    temp_wav = os.path.join(EXPORT_DIR, "_temp_audio.wav")

    # 检测设备
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    print(f"\n  正在加载阿里 FunASR 工业级模型 (设备: {device})...")

    try:
        # 🔥 稳定调用 Paraformer 模型，包含 VAD 和 标点控制
        model = AutoModel(
            model="iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            vad_model="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
            punc_model="iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
            device=device,
            disable_update=True  # 优先使用本地缓存
        )
    except Exception as e:
        print(f"[x] 模型加载失败: {e}")
        sys.exit(1)

    print(f"[OK] 模型加载完毕！开始处理 {len(video_tasks)} 个视频...\n")

    c = conn.cursor()
    success_count = 0
    total_segments_saved = 0

    for vid, path in tqdm(video_tasks, desc="ASR 转录进度"):
        base_name = os.path.basename(path)

        # 1. 提取单声道 wav
        if not extract_audio_ffmpeg(path, temp_wav):
            continue

        try:
            # 2. 极速推理 (funasr 内部会自动处理长音频切片)
            res = model.generate(input=temp_wav, batch_size_s=300)
            if not res or len(res) == 0: continue

            # 保存原始 JSON
            json_path = os.path.join(json_keep_dir, f"{base_name}.json")
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(res, f, ensure_ascii=False, indent=4)

            db_batch = []
            readable_lines = []

            # 阿里模型会将所有话连成一句，并自动加上完美的标点
            full_text = res[0].get('text', '')
            if not full_text.strip(): continue

            # 3. 根据标点进行物理断句
            short_sentences = split_to_sentences(full_text)

            for idx, sub in enumerate(short_sentences):
                # 防复读兜底
                if readable_lines and sub in readable_lines[-1]: continue

                # 存入数据库
                db_batch.append((vid, float(idx), sub))
                readable_lines.append(f"[句子 {idx + 1:03d}] {sub}")

            if db_batch:
                c.executemany("INSERT INTO text_segments (video_id, start_time, content) VALUES (?, ?, ?)", db_batch)
                total_segments_saved += len(db_batch)
                success_count += 1

            txt_path = os.path.join(EXPORT_DIR, f"{base_name}.asr.txt")
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(readable_lines))

        except Exception as e:
            tqdm.write(f"❌ 处理 {base_name} 时发生异常: {e}")

    # 清理临时音频文件
    if os.path.exists(temp_wav):
        try:
            os.remove(temp_wav)
        except:
            pass

    conn.commit()
    print(f"\n[Succ] ASR 提取阶段完成！成功处理 {success_count} 个视频，录入 {total_segments_saved} 句纯净台词。")


# ================= 🚀 终极光速版：ASR 匹配算法 =================
def run_matching(conn):
    c = conn.cursor()
    print("\n[Load] 正在加载台词库进行特征向量化...")

    c.execute("SELECT id, path, duration, size_bytes FROM videos WHERE status = 1")
    videos = {r[0]: {'id': r[0], 'path': r[1], 'dur': r[2], 'size': r[3], 'sentences': []} for r in c.fetchall()}

    c.execute("SELECT video_id, content FROM text_segments")
    for vid, content in c.fetchall():
        if vid in videos:
            videos[vid]['sentences'].append(content)

    data_list = [v for v in videos.values() if len(v['sentences']) > 1]
    data_list.sort(key=lambda x: (x['dur'], x['size']), reverse=True)

    if not data_list:
        print("[!] 暂无足够的台词数据进行比对。")
        return

    # 🚀 预处理阶段：缓存所有长度，生成哈希集合字典，杜绝在循环中重复计算
    for v in data_list:
        clean_items = []
        all_ngrams = set()
        text_set = set()  # 🌟 O(1) 精确匹配字典库

        for s in v['sentences']:
            cln = clean_text_for_match(s)
            length = len(cln)
            if length >= 2:
                ngrams = set(cln[k:k + 2] for k in range(length - 1))
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

            # 第一刀：宏观词组防碰瓷
            if not child['all_ngrams'] or not parent['all_ngrams']: continue
            video_overlap = len(child['all_ngrams'] & parent['all_ngrams']) / len(child['all_ngrams'])
            if video_overlap < 0.24:
                continue

            # 数学熔断器
            total_cs = len(child['clean_sentences'])
            required_matches = total_cs * TEXT_THRESHOLD
            max_allowed_misses = total_cs - required_matches

            matches = 0
            misses = 0

            for cs in child['clean_sentences']:
                # 第二刀：O(1) 哈希秒杀！
                if cs['text'] in parent_texts:
                    matches += 1
                    continue

                # 开始模糊查找
                best_score = 0
                for ps in parent['clean_sentences']:
                    if abs(cs['len'] - ps['len']) > 15: continue

                    min_len = min(cs['ngram_len'], ps['ngram_len'])
                    if min_len > 0:
                        if len(cs['ngrams'] & ps['ngrams']) / min_len < 0.3:
                            continue

                    score = difflib.SequenceMatcher(None, cs['text'], ps['text']).ratio()
                    if score > best_score:
                        best_score = score
                        if best_score >= SENT_SIM_LIMIT:
                            break

                if best_score >= SENT_SIM_LIMIT:
                    matches += 1
                else:
                    misses += 1
                    # 第三刀：无情熔断！
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
    print(f"\n[OK] 比对完成，共发现 {total_matches} 组重复内容。")


# ================= 主控制流 =================
def main():
    conn = init_db()

    c = conn.cursor()
    c.execute('''SELECT id, path FROM videos 
                 WHERE status = 1 AND id NOT IN (SELECT DISTINCT video_id FROM text_segments)''')
    all_pending = c.fetchall()

    if all_pending:
        random.shuffle(all_pending)
        print(f"  发现 {len(all_pending)} 个待转写视频 (已启用阿里 FunASR 工业流水线)。")
        with Timer(text="[T] run_native_asr_extraction 耗时: {:.4f}s"):
            run_native_asr_extraction(conn, all_pending)
    else:
        print("[Succ] 所有视频均已完成 ASR 特征提取。")

    with Timer(name="run_matching", text="[T] {name} 耗时: {:.4f}s"):
        run_matching(conn)
    conn.close()


if __name__ == '__main__':
    main()