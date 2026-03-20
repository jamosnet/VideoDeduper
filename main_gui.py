import tkinter as tk
from tkinter import ttk, messagebox, filedialog, Menu, Toplevel, simpledialog, Text, Scrollbar
import sqlite3
import os
import sys
import subprocess
import shutil
import re
import threading
import json
import difflib
import pickle
from gui_commander_sub1 import ExportMixin
from gui_commander_sub2 import ExportMixin2


__version__ = "1.0.4"

WORKSPACES_DIR = "workspaces"


def get_resource_path(relative_path):
    """
    获取资源的绝对路径 (例如 app.ico)。
    智能兼容源码环境、exe同级目录、以及 PyInstaller 6.x 的 _internal 目录。
    """
    if getattr(sys, 'frozen', False): # 【打包模式】：当前是 exe 运行
        # 1. 尝试从 exe 所在的同级目录找 (匹配你的 base_dir 逻辑) 比如你的 app.ico 和 main_gui.exe 放在同一个目录下
        base_dir = os.path.dirname(sys.executable)
        exe_dir_path = os.path.join(base_dir, relative_path)
        if os.path.exists(exe_dir_path):
            return exe_dir_path

        # 2. 尝试从 PyInstaller 6.x 默认的 _internal 数据目录找  当你在 spec 中写 datas=[('app.ico', '.')] 时，PyInstaller 会把它放进 _internal 里
        if hasattr(sys, '_MEIPASS'):
            internal_path = os.path.join(sys._MEIPASS, relative_path)
            if os.path.exists(internal_path):
                return internal_path

        return exe_dir_path # 兜底返回 (通常走到这里说明文件没拷过来)

    else:
        # 【源码模式】：当前是 .py 运行 直接相对当前项目根目录获取
        return os.path.join(os.path.abspath("."), relative_path)

class VideoDedupeApp(ExportMixin, ExportMixin2):
    def __init__(self, root):
        self.root = root
        self.root.title(f"视频去重指挥官 (多任务工作区版) V{__version__}")
        self.root.geometry("1400x850")
        
        # 👇 加上这两行，让窗口左上角和任务栏也显示你的图标
        icon_path = get_resource_path('app.ico')
        if os.path.exists(icon_path):
            self.root.iconbitmap(default=icon_path)
        if not os.path.exists(WORKSPACES_DIR):
            os.makedirs(WORKSPACES_DIR)

        self.current_config_path = None
        self.cfg = {}

        self.setup_ui()
        self.load_workspaces()
        self.root.after(100, self.check_dependencies)  # 👇 新增：在界面加载完成后，延迟 100 毫秒执行环境自检

    def setup_ui(self):
        # --- 顶部任务栏 (新增) ---
        frame_task = tk.Frame(self.root, pady=5, bg="#e0e0e0")
        frame_task.pack(fill=tk.X)

        tk.Label(frame_task, text="📂 当前任务:", bg="#e0e0e0", font=("Microsoft YaHei", 9, "bold")).pack(side=tk.LEFT, padx=5)
        self.combo_tasks = ttk.Combobox(frame_task, state="readonly", width=20)
        self.combo_tasks.pack(side=tk.LEFT, padx=5)
        self.combo_tasks.bind("<<ComboboxSelected>>", self.on_task_changed)

        tk.Button(frame_task, text="➕ 新建任务", command=self.create_new_task, bg="#c8e6c9").pack(side=tk.LEFT, padx=5)
        tk.Button(frame_task, text="⚙️ 任务设置", command=self.open_settings, bg="#bbdefb").pack(side=tk.LEFT, padx=5)        
        tk.Button(frame_task, text="❌️ 删除任务", command=self.delete_current_task, bg="#ffcdd2").pack(side=tk.LEFT, padx=5)
        tk.Label(frame_task, text=" | ").pack(side=tk.LEFT, padx=5)
        tk.Button(frame_task, text="🙋‍♂️ 关于作者 & 交流", command=self.show_about, bg="#fff9c4").pack(side=tk.LEFT, padx=5)
        
        # --- 控制区 ---
        frame_top = tk.Frame(self.root, pady=8)
        frame_top.pack(fill=tk.X)

        # 左侧：脚本执行区
        frame_scripts = tk.Frame(frame_top)
        frame_scripts.pack(side=tk.LEFT, padx=10)

        btn_style = {"padx": 10, "pady": 4, "width": 12}
        
        self.btn_build = tk.Button(frame_scripts, text="1. 建库/入库", command=lambda: self.run_script_in_thread("db_builder.py"), bg="#e1f5fe", **btn_style)
        self.btn_build.pack(side=tk.LEFT, padx=2)
        self.btn_audio = tk.Button(frame_scripts, text="2. 音频初筛", command=lambda: self.run_script_in_thread("audio_cleaner.py"), bg="#e8f5e9", **btn_style)
        self.btn_audio.pack(side=tk.LEFT, padx=2)
        self.btn_visual = tk.Button(frame_scripts, text="3. 视觉搜索", command=lambda: self.run_script_in_thread("visual_matcher.py"), bg="#fff3e0", **btn_style)
        self.btn_visual.pack(side=tk.LEFT, padx=2)
        self.btn_asr = tk.Button(frame_scripts, text="4. ASR 台词去重", command=lambda: self.run_script_in_thread("asr_processor.py"), bg="#f3e5f5", **btn_style)
        self.btn_asr.pack(side=tk.LEFT, padx=2)
        
        ttk.Separator(frame_top, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=5)

        # 中间：过滤控制区 (简化显示，保持逻辑)
        frame_filter = tk.LabelFrame(frame_top, text="视图过滤", padx=5, pady=2)
        frame_filter.pack(side=tk.LEFT, padx=5)

        self.filter_clip_var = tk.BooleanVar(value=False)
        self.chk_filter_clip = tk.Checkbutton(frame_filter, text="合集抓取模式", variable=self.filter_clip_var, command=self.load_data)
        self.chk_filter_clip.pack(side=tk.LEFT)

        tk.Label(frame_filter, text=" | ").pack(side=tk.LEFT, padx=5)
        self.show_all_var = tk.BooleanVar(value=False)
        self.chk_show_all = tk.Checkbutton(frame_filter, text="显示孤儿文件", variable=self.show_all_var, command=self.load_data)
        self.chk_show_all.pack(side=tk.LEFT)

        # 右侧：操作区
        frame_action = tk.Frame(frame_top)
        frame_action.pack(side=tk.RIGHT, padx=10)

        self.btn_manual_inspector = tk.Button(frame_action, text="🔍 人工溯源对比", command=lambda: self.run_script_in_thread("gui_manual_inspector.py"), **btn_style)
        self.btn_manual_inspector.pack(side=tk.LEFT, padx=5)

        self.btn_external_check = tk.Button(frame_action, text="🔍 外部单文件查重", command=self.run_external_check, bg="#e0f7fa", **btn_style)
        self.btn_external_check.pack(side=tk.LEFT, padx=5)        
        self.btn_batch_check = tk.Button(frame_action, text="📂 批量文件夹查重", command=self.run_batch_external_check, bg="#fff9c4", **btn_style)
        self.btn_batch_check.pack(side=tk.LEFT, padx=5)
        
        self.btn_refresh = tk.Button(frame_action, text="🔄 刷新列表", command=self.load_data, **btn_style)
        self.btn_refresh.pack(side=tk.LEFT, padx=5)

        self.btn_move = tk.Button(frame_action, text="✔️ 执行移动", command=self.move_duplicates, bg="#ffebee", fg="red", **btn_style)
        self.btn_move.pack(side=tk.LEFT, padx=5)

        self.all_buttons = [self.btn_build, self.btn_audio, self.btn_visual, self.btn_asr, self.btn_refresh, self.btn_move, self.chk_filter_clip, self.chk_show_all]

        # --- 列表区 (保持不变) ---
        frame_list = tk.Frame(self.root)
        frame_list.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        columns = ("status", "size", "duration", "res_bit", "info", "path", "db_id")
        self.tree = ttk.Treeview(frame_list, columns=columns, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="文件结构 (双击播放)", anchor=tk.W)
        self.tree.heading("status", text="状态"); self.tree.heading("size", text="大小")
        self.tree.heading("duration", text="时长"); self.tree.heading("res_bit", text="规格")
        self.tree.heading("info", text="相似度/备注"); self.tree.heading("path", text="路径"); self.tree.heading("db_id", text="ID")
        
        self.tree.column("#0", width=400); self.tree.column("status", width=60); self.tree.column("size", width=70, anchor=tk.E)
        self.tree.column("duration", width=70, anchor=tk.CENTER); self.tree.column("res_bit", width=120)
        self.tree.column("info", width=250); self.tree.column("path", width=100); self.tree.column("db_id", width=50, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(frame_list, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.tree.tag_configure("parent", foreground="green", font=("Microsoft YaHei", 9, "bold"))
        self.tree.tag_configure("child", foreground="red")
        self.tree.tag_configure("moved", foreground="gray")
        self.tree.tag_configure("orphan", foreground="#555555")

        # --- 事件绑定与右键菜单
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Button-3>", self.on_right_click)

        self.context_menu = Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="▶️ 播放选中视频", command=self.play_selected)
        self.context_menu.add_command(label="📂 打开文件所在位置", command=self.open_file_location)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="✏️ 重命名物理文件", command=self.rename_selected_file)
        self.context_menu.add_command(label="📄 复制文件名", command=self.copy_filename)
        self.context_menu.add_command(label="🔗 复制完整路径", command=self.copy_fullpath)        
        self.context_menu.add_command(label="✂️ 剪切选中文件到 [待确认/合集] 目录", command=self.manual_cut_files)
        self.context_menu.add_command(label="🛡️ 误判！改为保留 (Status=1)", command=lambda: self.update_status(1, "人工:保留"))
        self.context_menu.add_command(label="❌ 垃圾！标记删除 (Status=99)", command=lambda: self.update_status(99, "人工:待删"))
        self.context_menu.add_command(label="💔 解除关联！恢复为独立文件 (Status=1)", command=self.restore_independent)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="📝 查看视频ASR字幕内容", command=self.show_asr_text)  # 👇 新增：查看字幕菜单项
        self.context_menu.add_command(label="🎵 深度对比选中的2个文件(音频指纹)", command=self.run_audio_inspector_gui)
        self.context_menu.add_command(label="🔍 深度对比选中的2个文件(画面pHash)", command=self.run_inspector_gui)
        self.context_menu.add_command(label="🔍 深度对比选中的2个文件(语音ASR)", command=self.run_asr_inspector_gui)

        # --- 底部状态栏 ---
        self.status_var = tk.StringVar()
        self.status_var.set("请选择或新建一个任务...")
        tk.Label(self.root, textvariable=self.status_var, bd=1, relief=tk.SUNKEN, anchor=tk.W).pack(side=tk.BOTTOM, fill=tk.X)


    # ================== 核心：工作区与配置管理 ==================
    def load_workspaces(self):
        tasks = [d for d in os.listdir(WORKSPACES_DIR) if os.path.isdir(os.path.join(WORKSPACES_DIR, d))]
        self.combo_tasks['values'] = tasks
        if tasks:
            self.combo_tasks.current(0)
            self.on_task_changed()
        else:
            self.toggle_buttons(tk.DISABLED)

    def on_task_changed(self, event=None):
        task_name = self.combo_tasks.get()
        if not task_name: return
        self.current_config_path = os.path.join(WORKSPACES_DIR, task_name, "config.json")
        try:
            with open(self.current_config_path, "r", encoding="utf-8") as f:
                self.cfg = json.load(f)
            self.toggle_buttons(tk.NORMAL)
            self.load_data()
        except Exception as e:
            messagebox.showerror("错误", f"无法加载配置: {e}")

    def create_new_task(self):
        win = Toplevel(self.root)
        win.title("新建任务")
        win.geometry("400x300")

        # 👇 增加这两行：让这个子窗口永远浮在主窗口上面，并且拦截对主窗口的点击
        win.transient(self.root)
        win.grab_set()
        
        tk.Label(win, text="任务名称 (如 '小品'):").pack(pady=5)
        ent_name = tk.Entry(win, width=30)
        ent_name.pack()

        tk.Label(win, text="素材源目录 (选择包含视频的文件夹):").pack(pady=5)
        frame_dir = tk.Frame(win)
        frame_dir.pack()
        ent_dir = tk.Entry(frame_dir, width=25, state='readonly')
        ent_dir.pack(side=tk.LEFT, padx=5)
        
        def pick_dir():
            d = filedialog.askdirectory(parent=win)
            if d:
                ent_dir.config(state=tk.NORMAL)
                ent_dir.delete(0, tk.END)
                ent_dir.insert(0, os.path.normpath(d))
                ent_dir.config(state='readonly')

                dir_name = os.path.basename(d) # 获取目录名称并填充到 ent_name
                if not ent_name.get():  # 如果 ent_name 为空，才填充
                    ent_name.insert(0, dir_name)


            # 👇 增加这两行：选完目录后，强制把小窗口拉回最顶层并获取光标焦点
            win.lift()
            win.focus_force()
            
        tk.Button(frame_dir, text="浏览...", command=pick_dir).pack(side=tk.LEFT)

        def save():
            name = ent_name.get().strip()
            src = ent_dir.get().strip()
            if not name or not src: return messagebox.showwarning("提示", "请填写完整")
            
            task_dir = os.path.join(WORKSPACES_DIR, name)
            if os.path.exists(task_dir): return messagebox.showerror("错误", "任务名已存在")
            
            os.makedirs(task_dir)
            
            # 🔥 核心推导逻辑：防跨盘的路径生成
            safe_suffix = "_待确认"
            dup_dir = f"{src}{safe_suffix}\\_Duplicates_Final"
            man_dir = f"{src}{safe_suffix}\\_Manual_Sort"
            
            new_cfg = {
                "TASK_NAME": name,
                "SOURCE_DIR": src,
                "DB_FILE": os.path.join(task_dir, "video_library.db").replace('\\','/'),
                "DUPLICATE_DIR": dup_dir,
                "MANUAL_DIR": man_dir,
                "MAX_PROCESSES": 3,
                "SAMPLE_INTERVAL": 3,
                "AUDIO_THRESHOLD": 0.25,
                "VISUAL_COVERAGE": 0.6,
                "HAMMING_TOLERANCE": 9,
                
                "ASR_EXPORT_DIR": os.path.join(task_dir, "asr_texts").replace('\\','/'),
                "ASR_MODEL": "small",
                "ASR_BATCH_SIZE": 50,
                "ASR_TEXT_THRESHOLD": 0.6,
                "SENTENCE_SIMILARITY": 0.8,
                
                "SAFE_DURATION_DIFF": 30
            }
            
            with open(os.path.join(task_dir, "config.json"), "w", encoding="utf-8") as f:
                json.dump(new_cfg, f, ensure_ascii=False, indent=4)
                
            win.destroy()
            self.load_workspaces()
            self.combo_tasks.set(name)
            self.on_task_changed()

        tk.Button(win, text="创建任务", command=save, bg="#c8e6c9", font=("Arial", 10, "bold")).pack(pady=20)

    def open_settings(self):
        if not self.current_config_path: return
        win = Toplevel(self.root)
        win.title(f"任务设置: {self.cfg.get('TASK_NAME', '')}")
        win.geometry("550x580")
        
        entries = {}
        row = 0
        for k, v in self.cfg.items():
            tk.Label(win, text=k, font=("Arial", 9, "bold")).grid(row=row, column=0, padx=10, pady=5, sticky="e")
            ent = tk.Entry(win, width=45)
            ent.insert(0, str(v))
            # 锁定路径，防止乱改跨盘
            if "DIR" in k or "FILE" in k or "NAME" in k:
                ent.config(state='readonly', fg="gray")
            ent.grid(row=row, column=1, padx=5, pady=5, sticky="w")
            entries[k] = ent
            row += 1
            
        def save():
            for k, ent in entries.items():
                if ent.cget('state') == 'normal':
                    val = ent.get()
                    if val.isdigit(): val = int(val)
                    elif val.replace('.','',1).isdigit(): val = float(val)
                    self.cfg[k] = val
            with open(self.current_config_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=4)
            messagebox.showinfo("成功", "参数已保存生效！")
            win.destroy()
            
        tk.Button(win, text="💾 保存并应用", command=save, bg="#c8e6c9", font=("Arial", 10, "bold")).grid(row=row, columnspan=2, pady=15)

    def delete_current_task(self):
        task_name = self.combo_tasks.get()
        if not task_name or not self.current_config_path:
            return messagebox.showwarning("提示", "当前没有选中任何任务。")

        # 尝试读取配置中的物理隔离路径
        dup_dir = self.cfg.get("DUPLICATE_DIR", "")
        man_dir = self.cfg.get("MANUAL_DIR", "")
        
        # 智能检测这些目录是否存在
        exist_dirs = []
        if dup_dir and os.path.exists(dup_dir): 
            exist_dirs.append(f" - 待删垃圾区: {dup_dir}")
        if man_dir and os.path.exists(man_dir): 
            exist_dirs.append(f" - 待确认合集: {man_dir}")

        # 组装安全提示信息
        msg = f"确定要彻底删除工作区任务【{task_name}】吗？\n\n⚠️ 这只会删除后台数据库和配置文件，你的源视频绝对安全！\n"
        if exist_dirs:
            msg += "\n💡 检测到硬盘上存在以下物理隔离目录，本操作【不会】删除它们，请根据需要自行前往手动清理：\n" + "\n".join(exist_dirs)

        # 弹窗确认
        if not messagebox.askyesno("🗑️ 删除任务确认", msg):
            return

        # 执行删除（删除 workspaces 下对应的专属文件夹）
        task_dir = os.path.join(WORKSPACES_DIR, task_name)
        try:
            import shutil
            shutil.rmtree(task_dir)
        except Exception as e:
            return messagebox.showerror("错误", f"删除失败，数据库文件可能正被后台脚本占用，请稍后再试。\n错误信息: {e}")

        # 清空界面状态，重新加载任务列表
        messagebox.showinfo("成功", f"任务【{task_name}】的数据已清理完毕。")
        self.current_config_path = None
        self.cfg = {}
        for item in self.tree.get_children(): 
            self.tree.delete(item)
        self.status_var.set("请选择或新建一个任务...")
        self.load_workspaces()

    # ================== 任务分发与数据加载 ==================
    def run_script_in_thread(self, script_name):
        if not self.current_config_path: 
            return messagebox.showerror("错误", "请先选择或新建一个任务！")

        # ================== 核心：智能环境判断 ==================
        if getattr(sys, 'frozen', False):
            # 【打包模式】：当前是 exe 运行
            # 把传进来的 "db_builder.py" 替换成 "v11_db_builder.exe"
            exe_name = script_name.replace('.py', '.exe')
            # 获取当前主 exe 所在的目录
            base_dir = os.path.dirname(sys.executable)
            target_path = os.path.join(base_dir, exe_name)
            
            # 执行命令：直接调用 exe，不加 python 解释器
            cmd = [target_path, self.current_config_path]
        else:
            # 【源码模式】：当前是 .py 运行
            target_path = script_name
            # 执行命令：用 python.exe 去跑 .py 脚本
            cmd = [sys.executable, target_path, self.current_config_path]

        # =======================================================

        if not os.path.exists(target_path): 
            return messagebox.showerror("错误", f"找不到执行文件:\n{target_path}")
        
        self.toggle_buttons(tk.DISABLED)
        self.status_var.set(f"正在后台运行 {os.path.basename(target_path)}...")
        
        def task():
            print(f"\n{'='*20} 启动任务: {os.path.basename(target_path)} {'='*20}")
            # 传入自适应拼接好的 cmd 列表
            subprocess.call(cmd)
            print(f"{'='*20} 任务结束 {'='*20}\n")
            self.root.after(0, lambda: self.finish_script(os.path.basename(target_path)))

        threading.Thread(target=task, daemon=True).start()


    def finish_script(self, script_name):
        self.toggle_buttons(tk.NORMAL)
        self.load_data()
        if "gui_manual_inspector" not in script_name:
            messagebox.showinfo("完成", f"{script_name} 运行完毕。")

    def toggle_buttons(self, state):
        for btn in self.all_buttons:
            try: btn.config(state=state)
            except: pass


    def load_data(self):
        db_file = self.cfg.get("DB_FILE")
        if not db_file or not os.path.exists(db_file):
            self.status_var.set("当前任务数据库尚未建立，请先运行 1.建库/入库")
            for item in self.tree.get_children(): self.tree.delete(item)
            return
            
        self.toggle_buttons(tk.DISABLED)
        for item in self.tree.get_children(): self.tree.delete(item)

        conn = sqlite3.connect(db_file)
        c = conn.cursor()
        c.execute("SELECT id, path, size_bytes, duration, width, height, bitrate, status, similarity_info FROM videos")
        rows = c.fetchall()
        conn.close()

        nodes = {}
        for r in rows:
            nodes[r[0]] = {'id': r[0], 'path': r[1], 'size': r[2], 'dur': r[3], 'spec': f"{r[4]}x{r[5]}", 'status': r[7], 'info': str(r[8] or ""), 'children':[]}

        parents_set = set()
        
        # 🔥 核心革命：第一遍扫描，纯依靠文本里的 ID_xxx 建立血缘关系，无视 status
        for vid, node in nodes.items():
            m = re.search(r"ID_(\d+)", node['info'])
            if m:
                pid = int(m.group(1))
                if pid in nodes:
                    nodes[pid]['children'].append(node)
                    parents_set.add(pid) # 被别人指向的，就是父节点

        # 整理出父节点列表
        parents = [nodes[pid] for pid in parents_set]
        
        # 整理出孤儿节点 (不是别人的父亲，也没认别人当爹的)
        orphans =[]
        for vid, node in nodes.items():
            if vid not in parents_set:
                m = re.search(r"ID_(\d+)", node['info'])
                if not (m and int(m.group(1)) in nodes):
                    orphans.append(node)

        filter_clips = self.filter_clip_var.get()
        diff_limit = self.cfg.get("SAFE_DURATION_DIFF", 30)

        # 🔥 第二遍扫描：渲染树状图
        for p in sorted(parents, key=lambda x: x['size'] or 0, reverse=True):
            visible_children = []
            for c in p['children']:
                if filter_clips and abs(p['dur'] - c['dur']) < diff_limit: continue
                visible_children.append(c)
            if not visible_children: continue

            # 动态显示父节点状态 (它可能已经被用户标为99了)
            p_st = p['status']
            p_st_text = "基准" if p_st == 3 else ("待删" if p_st == 99 else ("保留" if p_st == 1 else f"状态:{p_st}"))
            # 动态颜色标签
            p_tag = "child" if p_st == 99 else "parent"

            pid_ui = self.tree.insert("", "end", text=f"🛡️ {os.path.basename(p['path'])}", 
                                      values=(p_st_text, self.fmt_size(p['size']), self.fmt_time(p['dur']), p['spec'], p['info'], p['path'], p['id']), 
                                      tags=(p_tag,), open=True)
            
            for c in visible_children:
                c_st = c['status']
                c_st_text = "待删" if c_st == 99 else ("已移" if c_st == 100 else ("保留" if c_st == 1 else f"状态:{c_st}"))
                c_tag = "moved" if c_st == 100 else ("child" if c_st == 99 else "parent")
                
                self.tree.insert(pid_ui, "end", text=f"❌ {os.path.basename(c['path'])}", 
                                 values=(c_st_text, self.fmt_size(c['size']), self.fmt_time(c['dur']), c['spec'], c['info'], c['path'], c['id']), 
                                 tags=(c_tag,))

        if self.show_all_var.get() and not filter_clips:
            orphan_root = self.tree.insert("", "end", text=f"📁 未分组文件 ({len(orphans)})", open=True)
            for o in orphans:
                self.tree.insert(orphan_root, "end", text=os.path.basename(o['path']), values=(o['status'], self.fmt_size(o['size']), self.fmt_time(o['dur']), o['spec'], o['info'], o['path'], o['id']), tags=("orphan",))

        self.status_var.set(f"📊 数据加载完毕 | 任务: {self.cfg.get('TASK_NAME')} | 总数: {len(rows)}")
        self.toggle_buttons(tk.NORMAL)
        
        
    # ================== 补全：交互动作逻辑 ==================
    def on_double_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        #if region == "tree" or region == "cell": 
        #    self.play_selected()
        if region == "tree" or region == "cell": 
            try:
                item_id = self.tree.selection()[0]
                item = self.tree.item(item_id)
                if 'values' in item and len(item['values']) >= 6:
                    file_path = item['values'][5]
                    if file_path and os.path.exists(file_path):
                        os.startfile(file_path)
                    else:
                        messagebox.showwarning("错误", "文件不存在")
                return "break"
            except IndexError:
                pass
    def on_right_click(self, event):
        item_id = self.tree.identify_row(event.y)
        if item_id:
            if item_id not in self.tree.selection():
                self.tree.selection_set(item_id)
            self.context_menu.post(event.x_root, event.y_root)

    def get_selected_paths(self):
        return [str(self.tree.item(i)['values'][5]) for i in self.tree.selection() if len(self.tree.item(i)['values']) >= 6]

    def play_selected(self):
        paths = self.get_selected_paths()
        if paths and os.path.exists(paths[0]): os.startfile(paths[0])

    def open_file_location(self):
        paths = self.get_selected_paths()
        if paths and os.path.exists(paths[0]): subprocess.Popen(f'explorer /select,"{os.path.normpath(paths[0])}"')

    def copy_filename(self):
        f = self.get_selected_paths()
        if f: 
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join([os.path.basename(x) for x in f]))

    def copy_fullpath(self):
        f = self.get_selected_paths()
        if f:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join([x for x in f]))


    def manual_cut_files(self):
        man_dir = self.cfg.get("MANUAL_DIR")
        if not man_dir: return messagebox.showwarning("警告", "当前任务未配置 MANUAL_DIR (待确认目录)")
        
        selection = self.tree.selection()
        if not selection: return
        if not messagebox.askyesno("确认", f"将 {len(selection)} 个文件移入\n{man_dir}\n作为合集/待确认隔离？"): return
        
        if not os.path.exists(man_dir): os.makedirs(man_dir)
        conn = sqlite3.connect(self.cfg["DB_FILE"])
        cnt = 0
        for item_id in selection:
            vid = self.tree.item(item_id)['values'][6]
            path = self.tree.item(item_id)['values'][5]
            if os.path.exists(path):
                try:
                    dst = os.path.join(man_dir, os.path.basename(path))
                    if os.path.exists(dst): 
                        base, ext = os.path.splitext(dst)
                        dst = f"{base}_{os.urandom(2).hex()}{ext}"
                    shutil.move(path, dst)
                    conn.execute("UPDATE videos SET status=100, similarity_info='人工移出' WHERE id=?", (vid,))
                    cnt += 1
                    self.append_log(f"人工剪切 | ID:{vid} | {path} -> {dst}") # 👈 就加这一行                    
                except Exception as e: print(e)
        conn.commit()
        conn.close()
        self.load_data()
        messagebox.showinfo("完成", f"已成功移出 {cnt} 个文件到待确认区。")


    def update_status(self, new_status, action_tag):
        selection = self.tree.selection()
        if not selection: return
        
        conn = sqlite3.connect(self.cfg["DB_FILE"])
        c = conn.cursor()
        
        for item_id in selection:
            vid = self.tree.item(item_id)['values'][6]
            
            # 读取原来的 info，保护里面的 ID_xxx 纽带
            c.execute("SELECT similarity_info FROM videos WHERE id=?", (vid,))
            old_info = c.fetchone()[0] or ""
            
            # 清理掉以前可能打过的 [人工] 标签，加上新的标签，并保留算法比对信息
            clean_info = re.sub(r"^\[人工.*?\]\s*", "", old_info)
            new_info = f"[{action_tag}] {clean_info}"
            
            c.execute("UPDATE videos SET status=?, similarity_info=? WHERE id=?", (new_status, new_info, vid))
            
        conn.commit()
        conn.close()
        self.load_data()

    def restore_independent(self):
        selection = self.tree.selection()
        if not selection: return

        if not messagebox.askyesno("解除关联", "确定要解除选中文件的父子关联，将其恢复为独立的未分组文件吗？"):
            return

        conn = sqlite3.connect(self.cfg["DB_FILE"])
        c = conn.cursor()

        for item_id in selection:
            vid = self.tree.item(item_id)['values'][6]

            # 读取原来的 info，寻找并破坏 ID_xxx 纽带
            c.execute("SELECT similarity_info FROM videos WHERE id=?", (vid,))
            old_info = c.fetchone()[0] or ""

            # 1. 清理掉以前可能打过的 [人工:xxx] 标签
            clean_info = re.sub(r"^\[人工.*?\]\s*", "", old_info)
            # 2. 核心逻辑：抹除所有的 ID_xxx 纽带，使其不再指向任何人
            clean_info = re.sub(r"\s*ID_\d+\s*", " ", clean_info).strip()
            # 3. 加上解绑专属标签
            new_info = f"[人工:解绑] {clean_info}".strip()

            # 恢复为 1！保留指纹，等待下一次微调后的匹配！
            c.execute("UPDATE videos SET status=1, similarity_info=? WHERE id=?", (new_info, vid))

        conn.commit()
        conn.close()

        # 刷新界面
        self.load_data()

    def rename_selected_file(self):
        selection = self.tree.selection()
        if len(selection) != 1:
            messagebox.showwarning("提示", "请选中 单个文件 进行重命名。")
            return

        item_id = selection[0]
        vals = self.tree.item(item_id)['values']
        
        # 获取旧路径和数据库 ID (索引 5 是 path, 6 是 db_id)
        old_path = vals[5]
        vid = vals[6]

        if not os.path.exists(old_path):
            messagebox.showerror("错误", "物理文件不存在，无法重命名。")
            return

        # 拆分路径、文件名和后缀
        old_dir = os.path.dirname(old_path)
        old_name = os.path.basename(old_path)
        name_without_ext, ext = os.path.splitext(old_name)

        # 弹窗询问新名字 (默认填入旧名字)
        new_name_base = simpledialog.askstring(
            "重命名文件", 
            f"请输入新的文件名 (无需输入 {ext} 后缀):", 
            initialvalue=name_without_ext, 
            parent=self.root
        )

        # 如果用户点了取消，或者名字没变，直接返回
        if not new_name_base or new_name_base.strip() == name_without_ext:
            return

        # 拼装新路径
        new_name = new_name_base.strip() + ext
        new_path = os.path.join(old_dir, new_name)

        # 防护：检查是否重名
        if os.path.exists(new_path):
            messagebox.showerror("错误", "该目录下已存在同名文件，请换个名字！")
            return

        try:
            # 1. 物理重命名硬盘文件
            os.rename(old_path, new_path)

            # 2. 更新 SQLite 数据库中的路径
            conn = sqlite3.connect(self.cfg["DB_FILE"])
            conn.execute("UPDATE videos SET path=? WHERE id=?", (new_path, vid))
            conn.commit()
            conn.close()

            # 3. 刷新列表显示
            self.load_data()
            messagebox.showinfo("成功", f"✅ 文件已成功重命名并更新数据库！\n\n新文件名:\n{new_name}")

        except Exception as e:
            messagebox.showerror("重命名失败", f"发生错误: {str(e)}\n\n(提示: 文件可能正被播放器占用，请先关闭视频播放)")

    # 后续的文件物理移动（基于读取配置里的路径）
    def move_duplicates(self):
        dup_dir = self.cfg.get("DUPLICATE_DIR")
        if not dup_dir: return
        if not messagebox.askyesno("确认", f"移动红色文件到\n{dup_dir}？"): return
        
        conn = sqlite3.connect(self.cfg["DB_FILE"])
        c = conn.cursor()
        c.execute("SELECT id, path FROM videos WHERE status=99")
        if not os.path.exists(dup_dir): os.makedirs(dup_dir)
        cnt = 0
        for vid, path in c.fetchall():
            if os.path.exists(path):
                try:
                    dst = os.path.join(dup_dir, os.path.basename(path))
                    if os.path.exists(dst): dst = os.path.join(dup_dir, f"{vid}_{os.path.basename(path)}")
                    shutil.move(path, dst)
                    c.execute("UPDATE videos SET status=100 WHERE id=?", (vid,))
                    cnt += 1
                    self.append_log(f"批量移动 | ID:{vid} | {path} -> {dst}") # 👈 就加这一行                    
                except: pass
        conn.commit(); conn.close()
        messagebox.showinfo("完成", f"移动了 {cnt} 个文件")
        if cnt > 0:
            self.run_script_in_thread("db_builder.py")
        else:
            self.load_data()

    def run_inspector_gui(self):
        # ... (保持 v14/v15 代码一致，这里省略节省空间) ...
        # 请务必确保 compare_videos_detailed 等函数存在
        selection = self.tree.selection()
        if len(selection) != 2:
            messagebox.showwarning("提示", "请选中 2 个文件")
            return
        try: ids = [self.tree.item(i)['values'][6] for i in selection]
        except: return
        win = Toplevel(self.root)
        win.title("对比报告")
        win.geometry("700x600")
        txt = Text(win, font=("Consolas", 10))
        txt.pack(fill=tk.BOTH, expand=True)
        def worker():
            try: txt.insert(tk.END, self.compare_videos_detailed(ids[0], ids[1]))
            except Exception as e: txt.insert(tk.END, str(e))
        threading.Thread(target=worker, daemon=True).start()

    def compare_videos_detailed(self, id1, id2):
        conn = sqlite3.connect(self.cfg["DB_FILE"])
        c = conn.cursor()
        c.execute("SELECT id, path, width, height, bitrate, size_bytes, duration FROM videos WHERE id IN (?, ?)", (id1, id2))
        metas = {r[0]: r for r in c.fetchall()}
        c.execute("SELECT video_id, phash FROM visual_hashes WHERE video_id IN (?, ?)", (id1, id2))
        hashes = {id1: set(), id2: set()}
        for vid, h in c.fetchall(): hashes[vid].add(int(h, 16))
        conn.close()
        res = []
        for vid in [id1, id2]:
            m = metas.get(vid)
            name = os.path.basename(m[1]) if m else "Unknown"
            res.append(f"ID {vid}: {name}\n   {m[2]}x{m[3]} | {int(m[6])}s | {len(hashes[vid])} hashes")
        
        def calc(src, dst):
            match = 0
            src = list(src); dst = list(dst)
            step = 1 if len(src) < 1000 else 2
            cnt = 0
            for i in range(0, len(src), step):
                cnt += 1
                for h in dst:
                    if bin(src[i] ^ h).count('1') <= self.cfg["HAMMING_TOLERANCE"]:
                        match += 1; break
            return match / cnt if cnt else 0
            
        res.append("-" * 30)
        res.append(f"A->B: {calc(hashes[id1], hashes[id2]):.1%}")
        res.append(f"B->A: {calc(hashes[id2], hashes[id1]):.1%}")
        return "\n".join(res)


# ================== 新增：专属 ASR 台词对比面板 ==================
    def run_asr_inspector_gui(self):
        selection = self.tree.selection()
        if len(selection) != 2:
            messagebox.showwarning("提示", "请选中 2 个文件")
            return
        try: 
            ids = [self.tree.item(i)['values'][6] for i in selection]
        except: 
            return
            
        win = Toplevel(self.root)
        win.title("语音台词深度对比报告")
        win.geometry("750x700")
        
        # 加个滚动条
        scrollbar = tk.Scrollbar(win)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        txt = Text(win, font=("Consolas", 10), yscrollcommand=scrollbar.set)
        txt.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=txt.yview)
        
        def worker():
            try: 
                txt.insert(tk.END, self.compare_videos_asr(ids[0], ids[1]))
            except Exception as e: 
                txt.insert(tk.END, f"发生错误: {str(e)}")
                
        threading.Thread(target=worker, daemon=True).start()

    def compare_videos_asr(self, id1, id2):
        conn = sqlite3.connect(self.cfg["DB_FILE"])
        c = conn.cursor()
        
        c.execute("SELECT id, path, duration FROM videos WHERE id IN (?, ?)", (id1, id2))
        metas = {r[0]: r for r in c.fetchall()}
        
        texts = {id1:[], id2:[]}
        try:
            c.execute("SELECT video_id, content FROM text_segments WHERE video_id IN (?, ?) ORDER BY id", (id1, id2))
            for vid, txt in c.fetchall(): 
                texts[vid].append(txt)
        except sqlite3.OperationalError:
            pass
        conn.close()

        res =[]
        for vid in [id1, id2]:
            m = metas.get(vid)
            name = os.path.basename(m[1]) if m else "Unknown"
            res.append(f"ID {vid}: {name}\n   时长: {int(m[2])}s | 提取台词: {len(texts[vid])} 句")
            
        res.append("-" * 45)

        if not texts[id1] or not texts[id2]:
            res.append("⚠️ 其中一个或两个视频无有效台词，无法对比。")
            return "\n".join(res)

        # =========================================================
        # 核心：完全同步后台引擎的“按句比对”逻辑
        # =========================================================
        sim_limit = self.cfg.get("SENTENCE_SIMILARITY", 0.65)

        def clean_text(t):
            t = re.sub(r'[^\w\u4e00-\u9fa5]', '', t)
            t = re.sub(r'[嗯啊哦哎呀呢啦哈呗嘛]', '', t)
            return t.lower()

        items_A = [(s, clean_text(s)) for s in texts[id1]]
        items_B = [(s, clean_text(s)) for s in texts[id2]]
        
        items_A =[x for x in items_A if len(x[1]) >= 2]
        items_B =[x for x in items_B if len(x[1]) >= 2]

        if not items_A or not items_B:
            res.append("⚠️ 去除语气词后无有效对白，判断为不相干。")
            return "\n".join(res)

        match_A_count = 0
        matched_pairs_A =[]  # 收集实锤证据
        for orig_a, clean_a in items_A:
            best_score = 0
            best_orig_b = ""
            for orig_b, clean_b in items_B:
                if abs(len(clean_a) - len(clean_b)) > 15: continue
                score = difflib.SequenceMatcher(None, clean_a, clean_b).ratio()
                if score > best_score:
                    best_score = score
                    best_orig_b = orig_b
            
            if best_score >= sim_limit:
                match_A_count += 1
                matched_pairs_A.append((best_score, orig_a, best_orig_b))

        match_B_count = 0
        for orig_b, clean_b in items_B:
            best_score = 0
            for orig_a, clean_a in items_A:
                if abs(len(clean_a) - len(clean_b)) > 15: continue
                score = difflib.SequenceMatcher(None, clean_b, clean_a).ratio()
                if score > best_score:
                    best_score = score
            if best_score >= sim_limit:
                match_B_count += 1

        coverage_A = match_A_count / len(items_A)
        coverage_B = match_B_count / len(items_B)
        
        res.append("【核心指标：台词覆盖率】 (完全同步后台处理逻辑)")
        res.append(f"  -> A 在 B 中的覆盖率: {coverage_A:.1%} (A有 {len(items_A)} 句有效，匹配上 {match_A_count} 句)")
        res.append(f"  -> B 在 A 中的覆盖率: {coverage_B:.1%} (B有 {len(items_B)} 句有效，匹配上 {match_B_count} 句)")

        avg_coverage = (coverage_A + coverage_B) / 2
        res.append("\n💡 系统综合诊断:")
        if avg_coverage >= self.cfg.get("ASR_TEXT_THRESHOLD", 0.6):
            res.append("[🚨 确认重复] 两者台词大面积重合，后台将执行自动去重！")
        elif avg_coverage >= 0.3:
            res.append("  [⚠️ 部分重合] 剧本/主题疑似相同，但断句节奏或部分表述存在差异。后台判定为【安全(保留)】。")
        else:
            res.append("  [✅ 完全独立] 关联度极低。")

        # --- 实况证据展示 (按句子长度排序，先看长句实锤) ---
        if matched_pairs_A:
            # 按A句子的长度倒序排列，优先展示长台词，过滤掉"谢谢"这种短噪音对视觉的干扰
            matched_pairs_A.sort(key=lambda x: len(x[1]), reverse=True)
            display_count = min(15, len(matched_pairs_A))
            
            res.append("\n" + "=" * 15 + f" 🎯 算法实锤：展示最长的 {display_count} 组相似台词 " + "=" * 15)
            for idx, (score, txt_a, txt_b) in enumerate(matched_pairs_A[:display_count], 1):
                res.append(f"【匹配案例 {idx}】(相似度 {score:.1%})")
                res.append(f"  A说: {txt_a}")
                res.append(f"  B说: {txt_b}")
                res.append("-" * 30)

        # --- 回归：展示前 40 句用于肉眼排查 ---
        res.append("\n" + "▼" * 20 + " A 提取到的前40句台词 " + "▼" * 20)
        if texts[id1]:
            res.append("\n".join(texts[id1][:40]))
            if len(texts[id1]) > 40: res.append("... (后续省略)")
        else:
            res.append("(无对白)")

        res.append("\n" + "▼" * 20 + " B 提取到的前40句台词 " + "▼" * 20)
        if texts[id2]:
            res.append("\n".join(texts[id2][:40]))
            if len(texts[id2]) > 40: res.append("... (后续省略)")
        else:
            res.append("(无对白)")

        return "\n".join(res)

    # ================== 新增：专属 音频指纹(fpcalc) 对比面板 ==================
    def run_audio_inspector_gui(self):
        selection = self.tree.selection()
        if len(selection) != 2:
            messagebox.showwarning("提示", "请选中 2 个文件")
            return
        try: 
            ids = [self.tree.item(i)['values'][6] for i in selection]
        except: 
            return
            
        win = Toplevel(self.root)
        win.title("🎵 音频声学指纹深度对比报告")
        win.geometry("650x500")
        
        scrollbar = tk.Scrollbar(win)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        txt = Text(win, font=("Consolas", 10), yscrollcommand=scrollbar.set)
        txt.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=txt.yview)
        
        def worker():
            try: 
                txt.insert(tk.END, self.compare_videos_audio(ids[0], ids[1]))
            except Exception as e: 
                txt.insert(tk.END, f"发生错误: {str(e)}")
                
        threading.Thread(target=worker, daemon=True).start()

    def compare_videos_audio(self, id1, id2):
        conn = sqlite3.connect(self.cfg["DB_FILE"])
        c = conn.cursor()
        
        # 1. 查基础信息
        c.execute("SELECT id, path, duration FROM videos WHERE id IN (?, ?)", (id1, id2))
        metas = {r[0]: r for r in c.fetchall()}
        
        # 2. 查音频指纹 (反序列化解析)
        fps = {id1: set(), id2: set()}
        try:
            c.execute("SELECT video_id, fingerprint FROM audio_fingerprints WHERE video_id IN (?, ?)", (id1, id2))
            for vid, fp_blob in c.fetchall():
                if fp_blob:
                    fps[vid] = set(pickle.loads(fp_blob))
        except sqlite3.OperationalError:
            pass # 防崩：表可能尚未建立
        conn.close()

        res = []
        for vid in[id1, id2]:
            m = metas.get(vid)
            name = os.path.basename(m[1]) if m else "Unknown"
            res.append(f"ID {vid}: {name}\n   时长: {int(m[2])}s | 提取音频指纹: {len(fps[vid])} 个特征簇")
            
        res.append("-" * 45)

        if not fps[id1] or not fps[id2]:
            res.append("⚠️ 其中一个或两个视频暂无音频指纹数据。")
            res.append("   请确认视频是否包含声音，或者是否已运行[2. 音频初筛]。")
            return "\n".join(res)

        # =========================================================
        # 核心：完全同步后台音频初筛引擎的“集合包含(Subset)”逻辑
        # =========================================================
        set_A = fps[id1]
        set_B = fps[id2]
        
        len_A = len(set_A)
        len_B = len(set_B)
        
        # 计算交集
        intersection = len(set_A & set_B)
        
        # 计算互相的覆盖率
        coverage_A_in_B = intersection / len_A if len_A > 0 else 0
        coverage_B_in_A = intersection / len_B if len_B > 0 else 0

        audio_threshold = self.cfg.get("AUDIO_THRESHOLD", 0.25)

        res.append("【核心指标：声学特征重合率】")
        res.append(" (底层逻辑：无视画面，仅对比背景音、配乐、声纹的交集)")
        res.append(f"  -> A 的声音在 B 中的重合度: {coverage_A_in_B:.1%} ({intersection}/{len_A})")
        res.append(f"  -> B 的声音在 A 中的重合度: {coverage_B_in_A:.1%} ({intersection}/{len_B})")

        res.append("\n💡 系统综合诊断:")
        
        max_coverage = max(coverage_A_in_B, coverage_B_in_A)
        
        if max_coverage >= audio_threshold:
            if abs(metas[id1][2] - metas[id2][2]) < 15:
                res.append("  [🚨 声音完全一致] 两个视频不仅声音重合度极高，且时长相近。大概率是音轨完全照搬！")
            else:
                res.append(f"  [✂️ 音频剪辑片段] 一方使用了另一方 {max_coverage:.1%} 的音轨。大概率是截取了音频片段。")
        elif max_coverage >= 0.1:
            res.append(f"  [⚠️ 微量同源素材] 相似度 {max_coverage:.1%}。使用了相同的短音效或很短的 BGM 片段。")
        else:
            res.append(f"  [✅ 声音完全独立] 相似度 {max_coverage:.1%} (底噪水平)。两者的声音/配乐各自独立，毫无瓜葛。")

        return "\n".join(res)

    def show_about(self):
        about_win = Toplevel(self.root)
        about_win.title("关于 视频去重指挥官")
        about_win.geometry("400x320")
        about_win.transient(self.root)
        about_win.grab_set()
        
        # 核心：一行代码搞定居中！(主窗X坐标 + 主窗宽度一半 - 弹窗宽度一半)
        about_win.geometry(f"400x320+{self.root.winfo_x() + self.root.winfo_width()//2 - 200}+{self.root.winfo_y() + self.root.winfo_height()//2 - 160}")

        tk.Label(about_win, text="视频多模态去重引擎", font=("Microsoft YaHei", 16, "bold")).pack(pady=10)
        tk.Label(about_win, text=f"版本: v{__version__} (Open Source Edition)", fg="gray").pack()
        tk.Label(about_win, text="核心技术: 视觉pHash + 声学fpcalc + 阿里FunASR", fg="#555").pack(pady=5)
        
        ttk.Separator(about_win, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=20, pady=10)
        
        tk.Label(about_win, text="开发初衷：解决个人海量视频囤积与剪辑溯源问题\n开源发布，希望能帮助到有需要的朋友！", justify=tk.CENTER).pack()
        
        tk.Label(about_win, text="交流与反馈：", font=("Microsoft YaHei", 10, "bold")).pack(pady=10)
        
        # 使用 Text 控件方便别人复制你的链接或邮箱
        txt = tk.Text(about_win, height=3, width=50, font=("Consolas", 10), bg="#f0f0f0", bd=0)
        txt.pack()
        txt.insert(tk.END, "GitHub: https://github.com/jamosnet/VideoDeduper\n")
        txt.insert(tk.END, "邮箱: jamosnet@outlook.com\n")
        txt.insert(tk.END, "QQ: 8185250 (备注: GitHub去重)")
        txt.config(state=tk.DISABLED) # 设为只读但可复制


    def append_log(self, msg):
        """极简日志工具：自动带上时间并追加到工作区"""
        if not self.cfg.get("DB_FILE"): return
        log_file = os.path.join(os.path.dirname(self.cfg["DB_FILE"]), "move_history.log")
        import datetime
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().strftime('%y-%m-%d %H:%M:%S')}] {msg}\n")
            
    def fmt_size(self, b): return f"{b/1024/1024:.1f} MB" if b else ""
    def fmt_time(self, s): return f"{int(s)//60}:{int(s)%60:02d}" if s else ""

if __name__ == "__main__":
    root = tk.Tk()
    app = VideoDedupeApp(root)
    root.mainloop()