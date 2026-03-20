import tkinter as tk
from tkinter import ttk, messagebox, filedialog, Menu, Toplevel, simpledialog, Text, Scrollbar
import sqlite3

import os
import sys
from codetiming import Timer  # 引入计时器


class ExportMixin:

    # ================== 新增：全模态沙盒鉴定（带保存与深度台词展示） ==================
    def run_external_check(self):
        if not self.cfg.get("DB_FILE") or not os.path.exists(self.cfg["DB_FILE"]):
            return messagebox.showerror("错误", "当前数据库不存在，请先选择一个任务工作区！")

        file_path = filedialog.askopenfilename(
            title="选择要鉴定的外部视频", 
            filetypes=[("Video Files", "*.mp4 *.mkv *.avi *.mov *.flv *.wmv *.ts *.webm *.rm *.rmvb"), ("All Files", "*.*")]
        )
        if not file_path:
            return

        # 创建独立报告窗口
        win = Toplevel(self.root)
        win.title(f"🕵️ 沙盒查重: {os.path.basename(file_path)}")
        win.geometry("900x750")

        # 顶部工具栏 (放保存按钮)
        frame_top = tk.Frame(win, pady=5)
        frame_top.pack(fill=tk.X, padx=10)
        
        # 滚动文本框
        scrollbar = tk.Scrollbar(win)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        txt = Text(win, font=("Microsoft YaHei", 10), yscrollcommand=scrollbar.set)
        txt.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        scrollbar.config(command=txt.yview)

        # 保存报告功能
        def save_report():
            save_path = filedialog.asksaveasfilename(
                title="保存报告", 
                defaultextension=".txt",
                initialfile=f"查重报告_{os.path.basename(file_path)}.txt",
                filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")]
            )
            if save_path:
                try:
                    with open(save_path, "w", encoding="utf-8") as f:
                        f.write(txt.get("1.0", tk.END))
                    messagebox.showinfo("成功", "报告已成功保存！")
                except Exception as e:
                    messagebox.showerror("错误", f"保存失败: {e}")

        tk.Button(frame_top, text="💾 保存此查重报告", command=save_report, bg="#e8f5e9", font=("Microsoft YaHei", 9, "bold")).pack(side=tk.RIGHT)

        #txt.insert(tk.END, f"🎯 正在鉴定外部视频:\n【{os.path.basename(file_path)}】\n")
        txt.insert(tk.END, f"🎯 正在鉴定外部视频:\n【{file_path}】\n")
        txt.insert(tk.END, "=" * 70 + "\n")
        win.update()

        def worker():
            try:
                import subprocess
                import pickle
                import tempfile
                import re
                import difflib
                from PIL import Image
                import imagehash

                # ---------------------------------------------------------
                # 0. 加载主数据库
                # ---------------------------------------------------------
                txt.insert(tk.END, "⏳[0/3] 正在加载本地媒体库特征数据...\n")
                conn = sqlite3.connect(self.cfg["DB_FILE"])
                c = conn.cursor()
                
                c.execute("SELECT id, path FROM videos")
                db_metas = {r[0]: r[1] for r in c.fetchall()}

                # 💡 升级版打印函数：支持 Top10，且支持打印匹配的实锤句子
                def print_topN(results, title_icon, n=10):
                    results.sort(key=lambda x: x[0], reverse=True)
                    if not results:
                        txt.insert(tk.END, f"   ✅ 未发现重合 (安全)。\n")
                    else:
                        for i, item in enumerate(results[:n], 1):
                            score = item[0]
                            vid = item[1]
                            path = db_metas.get(vid, 'Unknown')
                            txt.insert(tk.END, f"\n   🔥 [Top {i}] 重合度: {score:.1%} | {os.path.basename(path)}\n")
                            
                            # 如果有附带的实锤证据( matched_pairs )，打印出来
                            if len(item) > 2 and item[2]:
                                matches = item[2]
                                # 按外部台词的长度倒序，把长句顶在前面，避免被“你好”刷屏
                                matches.sort(key=lambda x: len(x[1]), reverse=True)
                                # 抽出最长的前 8 句作为实锤展示
                                for m_score, ext_orig, db_orig in matches[:8]:
                                    txt.insert(tk.END, f"      ↳ (相似度{m_score:.0%}) 外: {ext_orig}\n")
                                    txt.insert(tk.END, f"                   库: {db_orig}\n")
                                    txt.insert(tk.END, f"      {'-'*40}\n")
                    txt.see(tk.END)

                # ---------------------------------------------------------
                # 1. 音频指纹比对 (fpcalc)
                # ---------------------------------------------------------
                txt.insert(tk.END, "\n🎵 [1/3] 正在提取并比对音频声学指纹...\n")
                cmd_audio =['fpcalc', '-raw', '-length', '600', file_path]
                proc = subprocess.Popen(cmd_audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                out, _ = proc.communicate()
                
                if 'FINGERPRINT=' in out:
                    ext_fp_audio = set(int(x) for x in out.split('FINGERPRINT=')[1].strip().split(',') if x)
                    ext_audio_list = list(ext_fp_audio)
                    
                    c.execute("SELECT video_id, fingerprint FROM audio_fingerprints")
                    results_audio =[]
                    for vid, fp_blob in c.fetchall():
                        if not fp_blob: continue
                        db_fp = set(pickle.loads(fp_blob))
                        if len(ext_fp_audio & db_fp) / max(1, len(ext_fp_audio)) > 0.02:
                            matched = 0
                            db_list = list(db_fp)
                            for ha in ext_audio_list:
                                for hb in db_list:
                                    if bin(ha ^ hb).count('1') <= 2:
                                        matched += 1; break
                            score = matched / len(ext_audio_list) if ext_audio_list else 0
                            if score > 0.1: results_audio.append((score, vid))
                    print_topN(results_audio, "🎵 音频", 10)
                else:
                    txt.insert(tk.END, "   ⚠️ 提取音频失败，可能为静音视频。\n")

                # ---------------------------------------------------------
                # 2. 视觉画面比对 (pHash)
                # ---------------------------------------------------------
                txt.insert(tk.END, "\n👁️ [2/3] 正在提取并比对视觉画面特征...\n")
                temp_dir = tempfile.mkdtemp()
                subprocess.run(['ffmpeg', '-y', '-i', file_path, '-vf', 'fps=1,scale=-1:144', os.path.join(temp_dir, 'thumb_%04d.jpg')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                ext_phash = set()
                for f in os.listdir(temp_dir):
                    if f.endswith('.jpg'):
                        img_path = os.path.join(temp_dir, f)
                        try: ext_phash.add(int(str(imagehash.phash(Image.open(img_path))), 16))
                        except: pass
                        os.remove(img_path)
                os.rmdir(temp_dir)
                
                if ext_phash:
                    ext_vis_list = list(ext_phash)
                    c.execute("SELECT video_id, phash FROM visual_hashes")
                    db_visual = {}
                    for vid, h in c.fetchall(): db_visual.setdefault(vid, set()).add(int(h, 16))
                        
                    results_vis =[]
                    tol = self.cfg.get("HAMMING_TOLERANCE", 9)
                    for vid, db_hashes in db_visual.items():
                        matched = 0
                        db_list = list(db_hashes)
                        for ha in ext_vis_list:
                            for hb in db_list:
                                if bin(ha ^ hb).count('1') <= tol:
                                    matched += 1; break
                        score = matched / len(ext_vis_list) if ext_vis_list else 0
                        if score > 0.1: results_vis.append((score, vid))
                    print_topN(results_vis, "👁️ 视觉", 10)
                else:
                    txt.insert(tk.END, "   ⚠️ 提取画面失败。\n")

                # ---------------------------------------------------------
                # 3. 语音台词比对 (ASR)
                # ---------------------------------------------------------
                txt.insert(tk.END, "\n💬[3/3] 正在提取并比对语音台词 (需加载AI模型，请耐心等待)...\n")
                temp_wav = os.path.join(tempfile.gettempdir(), "_ext_temp_audio.wav")
                subprocess.run(['ffmpeg', '-y', '-i', file_path, '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', temp_wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                full_text = ""
                #try:
                #    from funasr import AutoModel
                #    import torch
                #    device = "cuda:0" if torch.cuda.is_available() else "cpu"
                #    model = AutoModel(
                #        model="iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
                #        vad_model="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                #        punc_model="iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
                #        device=device, disable_update=True
                #    )
                #    res = model.generate(input=temp_wav, batch_size_s=300)
                #    full_text = res[0].get('text', '') if res else ""
                #except Exception as e:
                #    txt.insert(tk.END, f"   ⚠️ ASR加载或推理失败: {e}\n")


                try:
                    import sys
                    from funasr_onnx import Fsmn_vad, Paraformer, CT_Transformer
                    import soundfile as sf
                    
                    # 1. 获取本地模型路径
                    if getattr(sys, 'frozen', False):
                        base_dir = os.path.dirname(sys.executable)
                    else:
                        base_dir = os.path.dirname(os.path.abspath(__file__))
                        
                    vad_model_dir = os.path.join(base_dir, "models", "speech_fsmn_vad_zh-cn-16k-common-pytorch")
                    asr_model_dir = os.path.join(base_dir, "models", "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch")
                    punc_model_dir = os.path.join(base_dir, "models", "punc_ct-transformer_zh-cn-common-vocab272727-pytorch")

                    # 2. 加载极速模型
                    vad_model = Fsmn_vad(vad_model_dir, quantize=True)
                    asr_model = Paraformer(asr_model_dir, batch_size=1, quantize=True)
                    punc_model = CT_Transformer(punc_model_dir, quantize=True)
                    
                    # 暴力提取文字函数
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

                    # 3. 运行 VAD 切片
                    vad_segments = vad_model([temp_wav])
                    if vad_segments and vad_segments[0]:
                        speech, sample_rate = sf.read(temp_wav)
                        # 为了安全，切片临时文件也放到系统的 temp 目录下
                        temp_chunk_path = os.path.join(tempfile.gettempdir(), "_ext_temp_chunk.wav")
                        full_text_list = []
                        
                        for segment in vad_segments[0]:
                            start_ms, end_ms = segment[0], segment[1]
                            start_idx = int(start_ms * sample_rate / 1000)
                            end_idx = int(end_ms * sample_rate / 1000)
                            audio_chunk = speech[start_idx:end_idx]
                            
                            sf.write(temp_chunk_path, audio_chunk, sample_rate)
                            
                            # ASR 识别
                            asr_res = asr_model([temp_chunk_path])
                            raw_text = extract_string(asr_res).strip()
                            
                            if raw_text:
                                # 加入标点
                                punc_res = punc_model(raw_text) # 加上列表扩号以防底层报错
                                punctuated_text = extract_string(punc_res) or raw_text
                                full_text_list.append(punctuated_text)
                                
                        if os.path.exists(temp_chunk_path):
                            try: os.remove(temp_chunk_path)
                            except: pass
                            
                        # 4. 完美拼接成带有标点符号的长字符串
                        full_text = "".join(full_text_list)

                except Exception as e:
                    txt.insert(tk.END, f"   ⚠️ ASR/ONNX加载或推理失败: {e}\n")


                if os.path.exists(temp_wav): os.remove(temp_wav)

                parts =[]
                if full_text:
                    def clean_text(t):
                        t = re.sub(r'[^\w\u4e00-\u9fa5]', '', t)
                        t = re.sub(r'[嗯啊哦哎呀呢啦哈呗嘛]', '', t)
                        return t.strip().lower()

                    parts = re.split(r'[，。！？；\s,.!?;…]+', full_text)
                    # 保留原句和洗稿后的纯净文本
                    ext_pairs =[(s, clean_text(s)) for s in parts if len(clean_text(s)) >= 2]
                    
                    c.execute("SELECT video_id, content FROM text_segments")
                    db_pairs = {}
                    for vid, txt_content in c.fetchall():
                        db_pairs.setdefault(vid,[]).append((txt_content, clean_text(txt_content)))
                        
                    results_asr =[]
                    sim_limit = self.cfg.get("SENTENCE_SIMILARITY", 0.65)
                    
                    if ext_pairs:
                        for vid, db_list in db_pairs.items():
                            matched_pairs =[]
                            # 做一个字典加速完全相等的查找
                            db_clean_to_orig = {clean: orig for orig, clean in db_list}
                            
                            for ext_orig, ext_clean in ext_pairs:
                                stop_words = {"对吧", "对不对", "好了", "太好了", "好吧", "是吧", "那个", "就是"}
                                if ext_clean in stop_words:  continue
                                # O(1) 极速秒杀
                                if ext_clean in db_clean_to_orig:
                                    matched_pairs.append((1.0, ext_orig, db_clean_to_orig[ext_clean]))
                                    continue
                                    
                                # difflib 高容错模糊匹配
                                best_score = 0
                                best_db_orig = ""
                                for db_orig, db_clean in db_list:
                                    if abs(len(ext_clean) - len(db_clean)) > 15: continue
                                    score = difflib.SequenceMatcher(None, ext_clean, db_clean).ratio()
                                    if score > best_score:
                                        best_score = score
                                        best_db_orig = db_orig
                                        if best_score >= sim_limit: break
                                        
                                if best_score >= sim_limit:
                                    matched_pairs.append((best_score, ext_orig, best_db_orig))
                                    
                            score = len(matched_pairs) / len(ext_pairs)
                            if score > 0.08:  # 放宽到 8% 就进入嫌疑榜，方便调试
                                results_asr.append((score, vid, matched_pairs))
                                
                        print_topN(results_asr, "💬 语音", 10)
                else:
                    if "⚠️ ASR加载或推理失败" not in txt.get("1.0", tk.END):
                        txt.insert(tk.END, "   ⚠️ 未提取到任何有效台词对白。\n")

                # ---------------------------------------------------------
                # 4. 附加完整文稿
                # ---------------------------------------------------------
                if full_text and parts:
                    txt.insert(tk.END, "\n" + "=" * 70 + "\n")
                    txt.insert(tk.END, "📜 附：外部视频完整识别文稿 (便于人工溯源核对)\n")
                    txt.insert(tk.END, "-" * 70 + "\n")
                    sent_idx = 1
                    for s in parts:
                        if s.strip():
                            txt.insert(tk.END, f"[{sent_idx:03d}] {s.strip()}\n")
                            sent_idx += 1

                conn.close()
                txt.insert(tk.END, "\n" + "=" * 70 + "\n")
                txt.insert(tk.END, "🎉 鉴定完毕！如果需要归档，请点击右上角保存按钮。\n")
                txt.see(tk.END)

            except Exception as e:
                txt.insert(tk.END, f"\n❌ 发生严重异常: {str(e)}\n")

        import threading
        threading.Thread(target=worker, daemon=True).start()

    # ================== 新增：批量全模态沙盒鉴定 (ONNX 极速版) ==================
    @Timer(text="run_batch_external_check 执行时间: {:.4f} 秒")
    def run_batch_external_check(self):
        if not self.cfg.get("DB_FILE") or not os.path.exists(self.cfg["DB_FILE"]):
            return messagebox.showerror("错误", "当前数据库不存在，请先选择一个任务工作区！")

        # 1. 选择目录
        target_dir = filedialog.askdirectory(title="选择包含待鉴定视频的文件夹")
        if not target_dir: return

        video_exts = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".ts", ".webm", ".rm", ".rmvb")
        files_to_check = [os.path.join(target_dir, f) for f in os.listdir(target_dir) if
                          f.lower().endswith(video_exts)]

        if not files_to_check:
            return messagebox.showinfo("提示", "该目录下没找到视频文件。")

        # 2. 进度窗口
        win = Toplevel(self.root)
        win.title("🚀 批量查重进度 (ONNX版)")
        win.geometry("700x500")
        lbl_status = tk.Label(win, text="准备中...", pady=10, font=("Microsoft YaHei", 10, "bold"))
        lbl_status.pack()
        txt_log = Text(win, font=("Consolas", 9), padx=5, pady=5)
        txt_log.pack(fill=tk.BOTH, expand=True)

        def batch_worker():
            try:
                import subprocess, pickle, tempfile, re, difflib, os, sys
                from PIL import Image
                import imagehash
                import soundfile as sf
                from funasr_onnx import Fsmn_vad, Paraformer, CT_Transformer

                # --- A. 初始化：预加载数据库到内存 (加速比对) ---
                txt_log.insert(tk.END, "⏳ 正在预加载数据库特征...\n")
                conn = sqlite3.connect(self.cfg["DB_FILE"])
                c = conn.cursor()
                c.execute("SELECT id, path FROM videos")
                db_metas = {r[0]: r[1] for r in c.fetchall()}
                c.execute("SELECT video_id, fingerprint FROM audio_fingerprints")
                db_audios = [(v, set(pickle.loads(f))) for v, f in c.fetchall() if f]
                c.execute("SELECT video_id, phash FROM visual_hashes")
                db_visuals = {};
                [db_visuals.setdefault(v, set()).add(int(h, 16)) for v, h in c.fetchall()]

                def clean_text(t):
                    t = re.sub(r'[^\w\u4e00-\u9fa5]', '', t)
                    t = re.sub(r'[嗯啊哦哎呀呢啦哈呗嘛]', '', t)
                    return t.strip().lower()

                c.execute("SELECT video_id, content FROM text_segments")
                db_texts = {}
                for v, ct in c.fetchall(): db_texts.setdefault(v, []).append((ct, clean_text(ct)))

                # --- B. 初始化：加载 ONNX 模型 (只加载一次) ---
                txt_log.insert(tk.END, "🤖 正在加载 ONNX 极速模型...\n")
                win.update()
                base_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(
                    os.path.abspath(__file__))
                vad_m = Fsmn_vad(os.path.join(base_dir, "models", "speech_fsmn_vad_zh-cn-16k-common-pytorch"),
                                 quantize=True)
                asr_m = Paraformer(os.path.join(base_dir, "models",
                                                "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"),
                                   batch_size=1, quantize=True)
                pnc_m = CT_Transformer(
                    os.path.join(base_dir, "models", "punc_ct-transformer_zh-cn-common-vocab272727-pytorch"),
                    quantize=True)

                def extract_string(obj):
                    if isinstance(obj, str): return obj
                    if isinstance(obj, dict):
                        for k in ["text", "preds"]:
                            if obj.get(k): return extract_string(obj[k])
                        for v in obj.values():
                            res = extract_string(v)
                            if res: return res
                    if isinstance(obj, (list, tuple)) and obj: return extract_string(obj[0])
                    return ""

                # --- C. 循环处理 ---
                for idx, file_path in enumerate(files_to_check, 1):
                    file_name = os.path.basename(file_path)
                    report_path = file_path + "_报告.txt"

                    # 【断点续传判断】
                    if os.path.exists(report_path):
                        txt_log.insert(tk.END, f"⏩ [{idx}/{len(files_to_check)}] 跳过已存在: {file_name}\n")
                        txt_log.see(tk.END);
                        win.update();
                        continue

                    lbl_status.config(text=f"正在处理 ({idx}/{len(files_to_check)}): {file_name}")
                    txt_log.insert(tk.END, f"▶️ 正在鉴定: {file_name}...")
                    txt_log.see(tk.END);
                    win.update()

                    report_content = [f"🎯 鉴定报告: {file_name}", f"📂 路径: {file_path}", "=" * 70]

                    # 1. 音频比对
                    res_audio = []
                    cmd = ['fpcalc', '-raw', '-length', '600', file_path]
                    out = subprocess.run(cmd, capture_output=True, text=True).stdout
                    if 'FINGERPRINT=' in out:
                        ext_fp = set(int(x) for x in out.split('FINGERPRINT=')[1].strip().split(',') if x)
                        for vid, db_fp_set in db_audios:
                            if len(ext_fp & db_fp_set) / max(1, len(ext_fp)) > 0.02:
                                matched = sum(
                                    1 for ha in ext_fp if any(bin(ha ^ hb).count('1') <= 2 for hb in db_fp_set))
                                score = matched / len(ext_fp) if ext_fp else 0
                                if score > 0.1: res_audio.append((score, vid))

                    # 2. 视觉比对
                    res_vis = []
                    t_dir = tempfile.mkdtemp()
                    subprocess.run(['ffmpeg', '-y', '-i', file_path, '-vf', 'fps=1,scale=-1:144',
                                    os.path.join(t_dir, 'th_%04d.jpg')], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                    ext_v = [int(str(imagehash.phash(Image.open(os.path.join(t_dir, f)))), 16) for f in
                             os.listdir(t_dir)]
                    import shutil;
                    shutil.rmtree(t_dir)
                    if ext_v:
                        tol = self.cfg.get("HAMMING_TOLERANCE", 9)
                        for vid, db_h_set in db_visuals.items():
                            matched = sum(
                                1 for ha in ext_v if any(bin(ha ^ hb).count('1') <= tol for hb in db_h_set))
                            score = matched / len(ext_v)
                            if score > 0.1: res_vis.append((score, vid))

                    # 3. ASR 比对 (ONNX 流程)
                    res_asr = [];
                    full_text_list = [];
                    parts = []
                    temp_wav = os.path.join(tempfile.gettempdir(), f"_bt_{idx}.wav")
                    subprocess.run(
                        ['ffmpeg', '-y', '-i', file_path, '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
                         temp_wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    if os.path.exists(temp_wav):
                        vad_segs = vad_m([temp_wav])
                        if vad_segs and vad_segs[0]:
                            speech, sr = sf.read(temp_wav)
                            t_chunk = os.path.join(tempfile.gettempdir(), "_chunk.wav")
                            for seg in vad_segs[0]:
                                chunk = speech[int(seg[0] * sr / 1000):int(seg[1] * sr / 1000)]
                                sf.write(t_chunk, chunk, sr)
                                raw = extract_string(asr_m([t_chunk])).strip()
                                if raw: full_text_list.append(extract_string(pnc_m(raw)) or raw)
                            if os.path.exists(t_chunk): os.remove(t_chunk)
                        os.remove(temp_wav)

                    full_text = "".join(full_text_list)
                    if full_text:
                        parts = [s.strip() for s in re.split(r'[，。！？；\s,.!?;…]+', full_text) if s.strip()]
                        ext_pairs = [(s, clean_text(s)) for s in parts if len(clean_text(s)) >= 2]
                        sim_limit = self.cfg.get("SENTENCE_SIMILARITY", 0.65)
                        for vid, db_list in db_texts.items():
                            matched_p = []
                            db_c2o = {c: o for o, c in db_list}
                            for e_o, e_c in ext_pairs:
                                if e_c in db_c2o:
                                    matched_p.append((1.0, e_o, db_c2o[e_c]))
                                else:
                                    for d_o, d_c in db_list:
                                        if abs(len(e_c) - len(d_c)) > 15: continue
                                        s = difflib.SequenceMatcher(None, e_c, d_c).ratio()
                                        if s >= sim_limit: matched_p.append((s, e_o, d_o)); break
                            score = len(matched_p) / len(ext_pairs) if ext_pairs else 0
                            if score > 0.08: res_asr.append((score, vid, matched_p))

                    # 4. 汇总报告并保存
                    def add_res(results, title):
                        report_content.append(f"\n[{title}比对]")
                        results.sort(key=lambda x: x[0], reverse=True)
                        if not results: report_content.append("  ✅ 安全 (无重合)")
                        for i, itm in enumerate(results[:5], 1):
                            report_content.append(
                                f"  🔥 Top {i} ({itm[0]:.1%}) -> {os.path.basename(db_metas.get(itm[1], '??'))}")
                            if len(itm) > 2:
                                for m_s, e_o, d_o in itm[2][:3]: report_content.append(
                                    f"     ↳ (似:{m_s:.0%}) 外:{e_o} / 库:{d_o}")

                    add_res(res_audio, "音频")
                    add_res(res_vis, "视觉")
                    add_res(res_asr, "语音")

                    if parts:
                        report_content.append("\n" + "=" * 45)
                        report_content.append(f"📊 共提取到 {len(parts)} 句台词：")
                        report_content.append("=" * 45 + "\n")
                        for p_i, p_s in enumerate(parts, 1): report_content.append(f"[{p_i:03d}]  {p_s}")

                    with open(report_path, "w", encoding="utf-8") as f:
                        f.write("\n".join(report_content))
                    txt_log.insert(tk.END, " 完成! ✅\n")

                conn.close()
                txt_log.insert(tk.END, "\n" + "★" * 20 + "\n批量任务全部完成！\n")
                messagebox.showinfo("完成", "所有视频比对任务已结束。")
            except Exception as e:
                txt_log.insert(tk.END, f"\n❌ 发生异常: {e}\n")

        import threading
        threading.Thread(target=batch_worker, daemon=True).start()


    # ================== 新增：查看单文件ASR字幕内容 ==================
    def show_asr_text(self):
        selection = self.tree.selection()
        if len(selection) != 1:
            messagebox.showwarning("提示", "请选中【单个】文件查看字幕。")
            return

        item_id = selection[0]
        vals = self.tree.item(item_id)['values']

        # 获取文件路径和数据库 ID (索引 5 是 path, 6 是 db_id)
        vid = vals[6]
        file_name = os.path.basename(vals[5])

        db_file = self.cfg.get("DB_FILE")
        if not db_file or not os.path.exists(db_file):
            return messagebox.showerror("错误", "当前任务数据库不存在！")

        # 连接数据库读取该视频的字幕
        conn = sqlite3.connect(db_file)
        c = conn.cursor()
        try:
            c.execute("SELECT content FROM text_segments WHERE video_id = ? ORDER BY id", (vid,))
            rows = c.fetchall()
        except sqlite3.OperationalError:
            # 防崩：如果 text_segments 表还没建立
            rows = []
        conn.close()

        # 创建独立文本弹窗
        win = Toplevel(self.root)
        win.title(f"ASR字幕内容 - {file_name}")
        win.geometry("500x650")

        # 居中显示
        win.geometry(
            f"+{self.root.winfo_x() + self.root.winfo_width() // 2 - 250}+{self.root.winfo_y() + self.root.winfo_height() // 2 - 325}")

        # 添加滚动条和文本框
        scrollbar = tk.Scrollbar(win)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        txt = Text(win, font=("Microsoft YaHei", 10), yscrollcommand=scrollbar.set, padx=10, pady=10)
        txt.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=txt.yview)

        # 填入数据
        if not rows:
            txt.insert(tk.END,
                       "⚠️ 暂无台词数据。\n\n可能原因：\n1. 尚未对该视频运行[4. ASR 台词去重]\n2. 视频中确实没有识别到任何有效人声对白。")
        else:
            txt.insert(tk.END, f"📊 共提取到 {len(rows)} 句台词：\n")
            txt.insert(tk.END, "=" * 45 + "\n\n")
            for idx, row in enumerate(rows, 1):
                txt.insert(tk.END, f"[{idx:03d}]  {row[0]}\n")
                # txt.insert(tk.END, "-" * 45 + "\n")

        # 设为只读，但允许复制
        txt.config(state=tk.DISABLED)