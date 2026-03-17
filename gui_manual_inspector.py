import tkinter as tk
from tkinter import ttk, messagebox, Menu, Toplevel, Text
import sqlite3
import os
import sys
import json
import numpy as np
import threading
import subprocess
import shutil
import re
import difflib

# ================= 动态配置加载 =================
CFG = {
    "SOURCE_DIR": "E:\\vod",
    "MANUAL_DIR": "E:\\vod_待确认\\_Manual_Sort",
    "DB_FILE": "video_library.db",
    "MAX_PROCESSES": 3,
    "SAMPLE_INTERVAL": 3,
    "AUDIO_THRESHOLD": 0.5,
    "VISUAL_COVERAGE": 0.6,
    "HAMMING_TOLERANCE": 9,
    "ASR_TEXT_THRESHOLD": 0.6,  # 视频间60%句子重合判定为重复
    "SENTENCE_SIMILARITY": 0.65,  # 句子相似度(使用difflib，0.65即可完美兼容少字漏字)
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
            print(f"⚠️ 无法读取工作区配置: {e}")

SOURCE_DIR = CFG["SOURCE_DIR"]
DB_FILE = CFG["DB_FILE"]
MANUAL_DIR = CFG["MANUAL_DIR"]


# ===============================================

class ManualInspectorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("视频人工溯源对比器 (三维全库排查版 🎵音频/👁️视觉/💬台词)")
        self.root.geometry("1400x850")

        if not os.path.exists(MANUAL_DIR): os.makedirs(MANUAL_DIR)

        # 数据缓存
        self.videos_meta = {}
        self.visual_cache = {}  # 视觉 pHash 缓存
        self.asr_cache = {}  # ASR 台词缓存
        self.audio_cache = {}  # 音频指纹缓存

        self.is_cache_loaded = False
        self.current_tree = None

        self.setup_ui()
        self.load_cache_in_background()

    def setup_ui(self):
        # --- 顶部控制区 ---
        frame_top = tk.Frame(self.root, pady=5)
        frame_top.pack(fill=tk.X, padx=10)

        # 第一行：搜索与状态
        frame_row1 = tk.Frame(frame_top)
        frame_row1.pack(fill=tk.X, pady=5)

        tk.Label(frame_row1, text="🔍 文件名关键词: ", font=("Microsoft YaHei", 10, "bold")).pack(side=tk.LEFT)
        self.ent_search = tk.Entry(frame_row1, width=30, font=("Microsoft YaHei", 10))
        self.ent_search.pack(side=tk.LEFT, padx=5)
        self.ent_search.bind('<Return>', lambda event: self.search_videos())
        self.add_context_menu(self.ent_search)  # ---> 新增：为搜索输入框绑定右键菜单 <---

        self.btn_search = tk.Button(frame_row1, text="搜索", command=self.search_videos, bg="#e3f2fd", width=8)
        self.btn_search.pack(side=tk.LEFT, padx=5)

        self.lbl_cache_status = tk.Label(frame_row1, text="⏳ 正在将[视觉/台词/音频]特征加载到内存...", fg="orange",
                                         font=("Microsoft YaHei", 9))
        self.lbl_cache_status.pack(side=tk.RIGHT, padx=10)

        # 第二部分：三大比对引擎控制台 (使用 LabelFrame 区分)
        frame_engines = tk.Frame(frame_top)
        frame_engines.pack(fill=tk.X, pady=5)

        # 【1. 音频引擎】
        lf_audio = tk.LabelFrame(frame_engines, text="🎵 音频指纹", padx=5, pady=5)
        lf_audio.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

        tk.Label(lf_audio, text="显示下限%:").pack(side=tk.LEFT)
        self.ent_au_min = tk.Entry(lf_audio, width=4, justify=tk.CENTER)
        self.ent_au_min.insert(0, "5")
        self.ent_au_min.pack(side=tk.LEFT, padx=2)

        self.btn_match_audio = tk.Button(lf_audio, text="分析音频", command=self.run_audio_matching, bg="#fff3e0",
                                         state=tk.DISABLED)
        self.btn_match_audio.pack(side=tk.RIGHT, padx=5)


        # 【2. 视觉引擎】
        lf_visual = tk.LabelFrame(frame_engines, text="👁️ 视觉 (pHash)", padx=5, pady=5)
        lf_visual.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

        tk.Label(lf_visual, text="容差(0-64):").pack(side=tk.LEFT)
        self.ent_v_tol = tk.Entry(lf_visual, width=4, justify=tk.CENTER)
        self.ent_v_tol.insert(0, str(CFG["HAMMING_TOLERANCE"]))
        self.ent_v_tol.pack(side=tk.LEFT, padx=2)

        tk.Label(lf_visual, text="显示下限%:").pack(side=tk.LEFT)
        self.ent_v_min = tk.Entry(lf_visual, width=4, justify=tk.CENTER)
        self.ent_v_min.insert(0, "5")
        self.ent_v_min.pack(side=tk.LEFT, padx=2)

        self.btn_match_visual = tk.Button(lf_visual, text="分析视觉", command=self.run_visual_matching, bg="#e8f5e9",
                                          state=tk.DISABLED)
        self.btn_match_visual.pack(side=tk.RIGHT, padx=5)

        # 【3. ASR 台词引擎】
        lf_asr = tk.LabelFrame(frame_engines, text="💬 台词 (ASR)", padx=5, pady=5)
        lf_asr.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

        tk.Label(lf_asr, text="句似度(0-1):").pack(side=tk.LEFT)
        self.ent_a_sim = tk.Entry(lf_asr, width=5, justify=tk.CENTER)
        self.ent_a_sim.insert(0, str(CFG["SENTENCE_SIMILARITY"]))
        self.ent_a_sim.pack(side=tk.LEFT, padx=2)

        tk.Label(lf_asr, text="显示下限%:").pack(side=tk.LEFT)
        self.ent_a_min = tk.Entry(lf_asr, width=4, justify=tk.CENTER)
        self.ent_a_min.insert(0, "5")
        self.ent_a_min.pack(side=tk.LEFT, padx=2)

        self.btn_match_asr = tk.Button(lf_asr, text="分析台词", command=self.run_asr_matching, bg="#e3f2fd",
                                       state=tk.DISABLED)
        self.btn_match_asr.pack(side=tk.RIGHT, padx=5)


        # --- 分割面板 ---
        self.paned_window = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        self.paned_window.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # ====== 1. 搜索结果 ======
        frame_upper = tk.LabelFrame(self.paned_window, text="1. 搜索结果 (选中一个你想排查的视频)")
        self.paned_window.add(frame_upper, weight=1)

        cols_up = ("id", "name", "duration", "res_bit", "size", "path")
        self.tree_search = ttk.Treeview(frame_upper, columns=cols_up, show="headings", selectmode="extended")

        self.tree_search.heading("id", text="ID")
        self.tree_search.heading("name", text="文件名", anchor=tk.W)
        self.tree_search.heading("duration", text="时长")
        self.tree_search.heading("res_bit", text="规格")
        self.tree_search.heading("size", text="大小", anchor=tk.E)
        self.tree_search.heading("path", text="路径", anchor=tk.W)

        self.tree_search.column("id", width=50, anchor=tk.CENTER)
        self.tree_search.column("name", width=400)
        self.tree_search.column("duration", width=80, anchor=tk.CENTER)
        self.tree_search.column("res_bit", width=120, anchor=tk.CENTER)
        self.tree_search.column("size", width=80, anchor=tk.E)
        self.tree_search.column("path", width=300)

        scroll_up = ttk.Scrollbar(frame_upper, orient=tk.VERTICAL, command=self.tree_search.yview)
        self.tree_search.configure(yscroll=scroll_up.set)
        scroll_up.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_search.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.tree_search.bind("<<TreeviewSelect>>", self.on_search_select)
        self.tree_search.bind("<Double-1>", lambda e: self.on_double_click(self.tree_search))
        self.tree_search.bind("<Button-3>", lambda e: self.on_right_click(e, self.tree_search))

        # ====== 2. 匹配结果 ======
        frame_lower = tk.LabelFrame(self.paned_window, text="2. 分析计算结果 (双向覆盖越相似越靠前)")
        self.paned_window.add(frame_lower, weight=2)

        cols_down = ("sim", "detail", "id", "name", "duration", "res_bit", "size", "path")
        self.tree_match = ttk.Treeview(frame_lower, columns=cols_down, show="headings", selectmode="extended")

        self.tree_match.heading("sim", text="最高相似度")
        self.tree_match.heading("detail", text="双向覆盖详情", anchor=tk.W)
        self.tree_match.heading("id", text="ID")
        self.tree_match.heading("name", text="文件名", anchor=tk.W)
        self.tree_match.heading("duration", text="时长")
        self.tree_match.heading("res_bit", text="规格")
        self.tree_match.heading("size", text="大小", anchor=tk.E)
        self.tree_match.heading("path", text="路径", anchor=tk.W)

        self.tree_match.column("sim", width=100, anchor=tk.CENTER)
        self.tree_match.column("detail", width=300)
        self.tree_match.column("id", width=50, anchor=tk.CENTER)
        self.tree_match.column("name", width=350)
        self.tree_match.column("duration", width=80, anchor=tk.CENTER)
        self.tree_match.column("res_bit", width=120, anchor=tk.CENTER)
        self.tree_match.column("size", width=80, anchor=tk.E)
        self.tree_match.column("path", width=300)

        scroll_down = ttk.Scrollbar(frame_lower, orient=tk.VERTICAL, command=self.tree_match.yview)
        self.tree_match.configure(yscroll=scroll_down.set)
        scroll_down.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_match.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.tree_match.bind("<Double-1>", lambda e: self.on_double_click(self.tree_match))
        self.tree_match.bind("<Button-3>", lambda e: self.on_right_click(e, self.tree_match))

        self.tree_match.tag_configure("high", background="#c8e6c9")
        self.tree_match.tag_configure("mid", background="#fff9c4")
        self.tree_match.tag_configure("low", background="#ffebee")

        # --- 右键菜单 ---
        self.context_menu = Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="▶️ 播放选中视频", command=self.play_selected)
        self.context_menu.add_command(label="📂 打开文件位置", command=self.open_file_location)
        self.context_menu.add_command(label="🔗 复制完整路径", command=self.copy_fullpath)
        self.context_menu.add_command(label="📝 查看视频ASR字幕内容", command=self.show_asr_text)  # 👇 新增：查看字幕菜单项
        self.context_menu.add_separator()
        self.context_menu.add_command(label="✂️ 剪切选中的文件到 _Manual_Sort", command=self.manual_cut_files)

    def add_context_menu(self, widget):
        right_click_menu = tk.Menu(widget, tearoff=0)
        right_click_menu.add_command(label="剪切 (Cut)", command=lambda: widget.event_generate("<<Cut>>"))
        right_click_menu.add_command(label="复制 (Copy)", command=lambda: widget.event_generate("<<Copy>>"))
        right_click_menu.add_command(label="粘贴 (Paste)", command=lambda: widget.event_generate("<<Paste>>"))
        right_click_menu.add_separator()
        right_click_menu.add_command(label="全选 (Select All)", command=lambda: widget.event_generate("<<SelectAll>>"))

        def show_menu(event):
            widget.focus()
            right_click_menu.tk_popup(event.x_root, event.y_root)

        widget.bind("<Button-3>", show_menu)

    # ================= 数据加载引擎 =================

    def load_cache_in_background(self):
        def worker():
            if not os.path.exists(DB_FILE): return
            try:
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()

                # 1. 基础元数据
                c.execute("SELECT id, path, size_bytes, duration, width, height, bitrate FROM videos")
                for r in c.fetchall():
                    self.videos_meta[r[0]] = {
                        'path': r[1], 'size': r[2], 'dur': r[3],
                        'res_bit': f"{r[4]}x{r[5]}, {r[6] // 1000 if r[6] else 0}k"
                    }

                # 2. 视觉特征加载 (pHash)
                c.execute("SELECT video_id, phash FROM visual_hashes")
                temp_v_hashes = {}
                for vid, phash_str in c.fetchall():
                    if vid not in temp_v_hashes: temp_v_hashes[vid] = []
                    temp_v_hashes[vid].append(int(phash_str, 16))
                for vid, hashes in temp_v_hashes.items():
                    self.visual_cache[vid] = np.unique(np.array(hashes, dtype=np.uint64))

                # 3. 台词特征加载 (洗稿、断句、预计算 N-grams)
                try:
                    c.execute("SELECT video_id, content FROM text_segments")
                    for vid, content in c.fetchall():
                        if vid not in self.asr_cache:
                            self.asr_cache[vid] = {'clean_sentences': [], 'text_set': set(), 'all_ngrams': set()}

                        # 同步 asr_processor.py 的核心洗稿算法
                        cln = re.sub(r'[^\w\u4e00-\u9fa5]', '', content)
                        cln = re.sub(r'[嗯啊哦哎呀呢啦哈呗嘛]', '', cln).strip().lower()
                        length = len(cln)

                        if length >= 2:
                            ngrams = set(cln[k:k + 2] for k in range(length - 1)) if length > 2 else set(list(cln))
                            self.asr_cache[vid]['clean_sentences'].append({
                                'text': cln, 'len': length, 'ngrams': ngrams, 'ngram_len': len(ngrams)
                            })
                            self.asr_cache[vid]['all_ngrams'].update(ngrams)
                            self.asr_cache[vid]['text_set'].add(cln)
                except Exception as e:
                    print(f"ASR缓存预载失败 (可能尚未生成): {e}")

                # 4. 音频指纹加载
                try:
                    # 🔔 如果你的表名叫其他名字，请修改这里的 SQL
                    c.execute("SELECT video_id, fingerprint FROM audio_fingerprints")
                    temp_a_hashes = {}
                    for vid, fp in c.fetchall():
                        if vid not in temp_a_hashes: temp_a_hashes[vid] = []
                        temp_a_hashes[vid].append(fp)
                    for vid, fps in temp_a_hashes.items():
                        self.audio_cache[vid] = set(fps)  # 使用集合交集比对
                except Exception:
                    pass  # 忽略报错，可能还没有音频表

                conn.close()
                self.is_cache_loaded = True

                status_txt = f"✅ 缓存就绪 | 视觉:{len(self.visual_cache)} | 台词:{len(self.asr_cache)} | 音频:{len(self.audio_cache)}"
                self.root.after(0, lambda: self.lbl_cache_status.config(text=status_txt, fg="green"))

            except Exception as e:
                self.root.after(0, lambda: self.lbl_cache_status.config(text=f"❌ 缓存失败: {e}", fg="red"))

        threading.Thread(target=worker, daemon=True).start()

    # ================= UI 交互流 =================

    def set_buttons_state(self, state):
        self.btn_match_visual.config(state=state)
        self.btn_match_asr.config(state=state)
        self.btn_match_audio.config(state=state)

    def on_search_select(self, event):
        if self.tree_search.selection() and self.is_cache_loaded:
            self.set_buttons_state(tk.NORMAL)
        else:
            self.set_buttons_state(tk.DISABLED)

    def search_videos(self):
        keyword = self.ent_search.get().strip()
        if not os.path.exists(DB_FILE): return

        for item in self.tree_search.get_children(): self.tree_search.delete(item)

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        if keyword:
            c.execute("SELECT id, path, size_bytes, duration, width, height, bitrate FROM videos WHERE path LIKE ?",
                      (f"%{keyword}%",))
        else:
            c.execute("SELECT id, path, size_bytes, duration, width, height, bitrate FROM videos LIMIT 100")

        rows = c.fetchall()
        conn.close()

        for r in rows:
            res_bit = f"{r[4]}x{r[5]}, {r[6] // 1000 if r[6] else 0}k"
            vals = (r[0], os.path.basename(r[1]), self.fmt_time(r[3]), res_bit, self.fmt_size(r[2]), r[1])
            self.tree_search.insert("", "end", values=vals)

    # ================= 1. 👁️ 视觉比对引擎 =================
    def run_visual_matching(self):
        selection = self.tree_search.selection()
        if not selection: return
        target_id = int(self.tree_search.item(selection[0])['values'][0])

        if target_id not in self.visual_cache:
            messagebox.showwarning("警告", "该视频没有[视觉]指纹数据，无法匹配。")
            return

        target_arr = self.visual_cache[target_id]

        try:
            tolerance = int(self.ent_v_tol.get())
        except:
            tolerance = 9
        try:
            min_sim_display = float(self.ent_v_min.get()) / 100.0
        except:
            min_sim_display = 0.05

        for item in self.tree_match.get_children(): self.tree_match.delete(item)
        self.set_buttons_state(tk.DISABLED)
        self.btn_match_visual.config(text="⏳ 视觉矩阵运算中...")

        def worker():
            results = []
            for vid, cand_arr in self.visual_cache.items():
                if vid == target_id: continue
                if target_arr.size == 0 or cand_arr.size == 0: continue

                xor_matrix = np.bitwise_xor(target_arr[:, None], cand_arr[None, :])
                xor_uint8 = np.ascontiguousarray(xor_matrix)[..., None].view(np.uint8)
                bits = np.unpackbits(xor_uint8, axis=2)
                distances = bits.sum(axis=2)

                hits_t_in_c = np.any(distances <= tolerance, axis=1)
                cov_t_in_c = np.sum(hits_t_in_c) / target_arr.size

                hits_c_in_t = np.any(distances <= tolerance, axis=0)
                cov_c_in_t = np.sum(hits_c_in_t) / cand_arr.size

                max_sim = max(cov_t_in_c, cov_c_in_t)
                if max_sim >= min_sim_display:
                    results.append((vid, max_sim, cov_t_in_c, cov_c_in_t))

            results.sort(key=lambda x: x[1], reverse=True)
            self.root.after(0, lambda: self.render_match_results(results, target_id, "视觉"))

        threading.Thread(target=worker, daemon=True).start()

    # ================= 2. 💬 ASR 台词比对引擎 =================
    def run_asr_matching(self):
        selection = self.tree_search.selection()
        if not selection: return
        target_id = int(self.tree_search.item(selection[0])['values'][0])

        target_data = self.asr_cache.get(target_id)
        if not target_data or not target_data['clean_sentences']:
            messagebox.showwarning("警告", "该视频没有[台词]数据！请检查是否已完成 ASR 提取。")
            return

        try:
            sent_sim_limit = float(self.ent_a_sim.get())
        except:
            sent_sim_limit = 0.65
        try:
            min_sim_display = float(self.ent_a_min.get()) / 100.0
        except:
            min_sim_display = 0.05

        for item in self.tree_match.get_children(): self.tree_match.delete(item)
        self.set_buttons_state(tk.DISABLED)
        self.btn_match_asr.config(text="⏳ 台词模糊计算中...")

        def worker():
            results = []
            target_cs = target_data['clean_sentences']
            target_texts = target_data['text_set']
            total_t = len(target_cs)

            # 1对多全库遍历，由于有 O(1) 字典，1000 个视频只需 1~2 秒
            for vid, cand_data in self.asr_cache.items():
                if vid == target_id: continue
                cand_cs = cand_data['clean_sentences']
                total_c = len(cand_cs)
                if total_c == 0: continue

                # 计算 Target 包含在 Candidate 的比例
                matches_t = 0
                for ts in target_cs:
                    # O(1) 哈希秒杀原话
                    if ts['text'] in cand_data['text_set']:
                        matches_t += 1
                        continue

                    # 模糊寻找
                    best_score = 0
                    for cs in cand_cs:
                        if abs(ts['len'] - cs['len']) > 15: continue
                        # N-gram 极速微观防碰瓷
                        min_len = min(ts['ngram_len'], cs['ngram_len'])
                        if min_len > 0 and len(ts['ngrams'] & cs['ngrams']) / min_len < 0.3: continue

                        score = difflib.SequenceMatcher(None, ts['text'], cs['text']).ratio()
                        if score > best_score:
                            best_score = score
                            if best_score >= sent_sim_limit: break
                    if best_score >= sent_sim_limit:
                        matches_t += 1
                cov_t_in_c = matches_t / total_t

                # 计算 Candidate 包含在 Target 的比例
                matches_c = 0
                for cs in cand_cs:
                    if cs['text'] in target_texts:
                        matches_c += 1
                        continue
                    best_score = 0
                    for ts in target_cs:
                        if abs(cs['len'] - ts['len']) > 15: continue
                        min_len = min(cs['ngram_len'], ts['ngram_len'])
                        if min_len > 0 and len(cs['ngrams'] & ts['ngrams']) / min_len < 0.3: continue

                        score = difflib.SequenceMatcher(None, cs['text'], ts['text']).ratio()
                        if score > best_score:
                            best_score = score
                            if best_score >= sent_sim_limit: break
                    if best_score >= sent_sim_limit:
                        matches_c += 1
                cov_c_in_t = matches_c / total_c

                max_sim = max(cov_t_in_c, cov_c_in_t)
                if max_sim >= min_sim_display:
                    results.append((vid, max_sim, cov_t_in_c, cov_c_in_t))

            results.sort(key=lambda x: x[1], reverse=True)
            self.root.after(0, lambda: self.render_match_results(results, target_id, "台词"))

        threading.Thread(target=worker, daemon=True).start()

    # ================= 3. 🎵 音频比对引擎 (预留集合碰撞逻辑) =================
    def run_audio_matching(self):
        selection = self.tree_search.selection()
        if not selection: return
        target_id = int(self.tree_search.item(selection[0])['values'][0])

        target_fp = self.audio_cache.get(target_id)
        if not target_fp:
            messagebox.showwarning("警告",
                                   "该视频没有[音频]指纹数据！\n如果是未配置音频表，请在代码搜索'audio_fingerprints'进行字段修改。")
            return

        try:
            min_sim_display = float(self.ent_au_min.get()) / 100.0
        except:
            min_sim_display = 0.05

        for item in self.tree_match.get_children(): self.tree_match.delete(item)
        self.set_buttons_state(tk.DISABLED)
        self.btn_match_audio.config(text="⏳ 音频碰撞计算中...")

        def worker():
            results = []
            len_t = len(target_fp)

            for vid, cand_fp in self.audio_cache.items():
                if vid == target_id: continue
                len_c = len(cand_fp)
                if len_c == 0: continue

                # 通过集合交集，计算指纹重合度 (适合离散短指纹)
                intersection = len(target_fp & cand_fp)
                cov_t_in_c = intersection / len_t
                cov_c_in_t = intersection / len_c

                max_sim = max(cov_t_in_c, cov_c_in_t)
                if max_sim >= min_sim_display:
                    results.append((vid, max_sim, cov_t_in_c, cov_c_in_t))

            results.sort(key=lambda x: x[1], reverse=True)
            self.root.after(0, lambda: self.render_match_results(results, target_id, "音频"))

        threading.Thread(target=worker, daemon=True).start()

    # ================= 渲染结果与状态重置 =================
    def render_match_results(self, results, target_id, mode_name):
        self.set_buttons_state(tk.NORMAL)
        self.btn_match_visual.config(text="分析视觉")
        self.btn_match_asr.config(text="分析台词")
        self.btn_match_audio.config(text="分析音频")

        for vid, max_sim, cov_t_in_c, cov_c_in_t in results:
            meta = self.videos_meta.get(vid)
            if not meta: continue

            detail = f"[{mode_name}] 含目标 {cov_t_in_c:.1%} | 目标含它 {cov_c_in_t:.1%}"
            tag = "high" if max_sim >= 0.6 else "mid" if max_sim >= 0.3 else "low"

            vals = (
                f"{max_sim:.1%}", detail, vid,
                os.path.basename(meta['path']),
                self.fmt_time(meta['dur']),
                meta['res_bit'],
                self.fmt_size(meta['size']),
                meta['path']
            )
            self.tree_match.insert("", "end", values=vals, tags=(tag,))

    # --- 辅助与交互功能 ---
    def on_double_click(self, tree):
        selection = tree.selection()
        if selection:
            path = tree.item(selection[0])['values'][-1]
            if path and os.path.exists(path): os.startfile(path)

    def on_right_click(self, event, tree):
        item_id = tree.identify_row(event.y)
        if item_id:
            if item_id not in tree.selection():
                tree.selection_set(item_id)
            self.current_tree = tree
            self.context_menu.post(event.x_root, event.y_root)

    def play_selected(self):
        if self.current_tree and self.current_tree.selection():
            path = self.current_tree.item(self.current_tree.selection()[0])['values'][-1]
            if os.path.exists(path): os.startfile(path)

    def open_file_location(self):
        if self.current_tree and self.current_tree.selection():
            path = self.current_tree.item(self.current_tree.selection()[0])['values'][-1]
            if os.path.exists(path): subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')

    def copy_fullpath(self):
        if self.current_tree:
            files = [str(self.current_tree.item(i)['values'][-1]) for i in self.current_tree.selection()]
            if files:
                self.root.clipboard_clear()
                self.root.clipboard_append("\n".join(files))

    # ================== 新增：查看单文件ASR字幕内容 ==================
    def show_asr_text(self):
        selection = self.current_tree.selection()
        if len(selection) != 1:
            messagebox.showwarning("提示", "请选中【单个】文件查看字幕。")
            return

        item_id = selection[0]
        vals = self.current_tree.item(item_id)['values']

        if self.current_tree == self.tree_search: #
            vid = vals[0]
            file_name = os.path.basename(vals[1])
        else:
            vid = vals[2]
            file_name = os.path.basename(vals[3])


        if not DB_FILE or not os.path.exists(DB_FILE):
            return messagebox.showerror("错误", "当前任务数据库不存在！")

        # 连接数据库读取该视频的字幕
        conn = sqlite3.connect(DB_FILE)
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



    def manual_cut_files(self):
        if not self.current_tree: return
        selection = self.current_tree.selection()
        if not selection: return

        if not messagebox.askyesno("确认剪切",
                                   f"确定将选中的 {len(selection)} 个文件\n移动到 {MANUAL_DIR} 吗？\n(这也会将它们标记为已处理)"):
            return

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        count = 0

        for item_id in selection:
            vals = self.current_tree.item(item_id)['values']
            if self.current_tree == self.tree_search:
                vid = vals[0];
                path = vals[5]
            else:
                vid = vals[2];
                path = vals[7]

            if os.path.exists(path):
                try:
                    fname = os.path.basename(path)
                    dst = os.path.join(MANUAL_DIR, fname)
                    if os.path.exists(dst):
                        base, ext = os.path.splitext(fname)
                        dst = os.path.join(MANUAL_DIR, f"{base}_MANUAL_{os.urandom(2).hex()}{ext}")

                    shutil.move(path, dst)
                    c.execute("UPDATE videos SET status=100, similarity_info='人工移出' WHERE id=?", (vid,))
                    count += 1
                except Exception as e:
                    print(f"Error moving {path}: {e}")

            self.current_tree.delete(item_id)

        conn.commit()
        conn.close()
        messagebox.showinfo("完成", f"成功剪切了 {count} 个文件。")

    def fmt_size(self, b):
        return f"{b / 1024 / 1024:.1f} MB" if b else "0 MB"

    def fmt_time(self, s):
        return f"{int(s) // 60}:{int(s) % 60:02d}" if s else "0:00"


if __name__ == "__main__":
    root = tk.Tk()
    app = ManualInspectorApp(root)
    root.mainloop()