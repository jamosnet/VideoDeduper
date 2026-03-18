import tkinter as tk
from tkinter import ttk, messagebox, filedialog, Menu, Toplevel, simpledialog, Text, Scrollbar
import sqlite3

import os
import sys
import requests
import zipfile
import threading
import shutil


class ExportMixin2:

    # ================== 新增：环境自检与自动下载模块 ==================
    def check_dependencies(self):
        """检查核心依赖 (ffmpeg, ffprobe, fpcalc, models)"""
        # 获取当前运行的根目录
        base_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.abspath(".")

        missing_items =[]
        need_tools = False
        need_models = False

        depend_files = ['ffmpeg', 'ffprobe', 'fpcalc']
        # 1. 检查 EXE 工具 (先看环境变量，再看当前目录)
        for tool in depend_files:
            tool_exe = f"{tool}.exe"
            if not shutil.which(tool) and not os.path.exists(os.path.join(base_dir, tool_exe)):
                missing_items.append(tool_exe)
                need_tools = True

        # 2. 检查 AI 模型文件夹
        models_path = os.path.join(base_dir, 'models')
        #if not os.path.exists(models_path):
        if not os.path.exists(models_path) or self.get_folder_size(models_path) < 200 * 1024 * 1024:  # 200MB
            missing_items.append('models/ (AI语音识别模型)')
            need_models = True

        if not missing_items:
            print(f'所需依赖文件 { " ".join([f"{file}.exe" for file in depend_files]) } 已经存在')
            return  # 环境完整，直接返回正常运行

        # 根据缺失情况准备对应的下载链接
        download_urls =[]
        if need_tools:
            download_urls.append(("Tool.zip", "https://github.com/jamosnet/VideoDeduper/releases/download/v1.0.0-asset/Tool.zip"))
        if need_models:
            download_urls.append(("models.zip", "https://github.com/jamosnet/VideoDeduper/releases/download/v1.0.0-asset/models.zip"))

        # 发现缺失，弹窗提示
        msg = (
            f"⚠️ 检测到当前环境缺少以下核心依赖：\n\n"
            f"{chr(10).join(['  - ' + item for item in missing_items])}\n\n"
            f"由于包含 AI 离线模型及音视频处理工具，依赖包体积较大。\n"
            f"是否立即通过网络自动下载并配置？\n"
            f"(如果下载较慢，你也可以点击'否'，自行前往 GitHub 手动下载配置)"
        )

        if messagebox.askyesno("缺少核心依赖", msg):
            self.show_download_dialog(download_urls, base_dir)
        else:
            self.status_var.set("⚠️ 缺少核心依赖，部分核心功能 (如音频/台词去重) 将无法运行！")
            # 禁用相关按钮防止报错
            self.btn_audio.config(state=tk.DISABLED)
            self.btn_asr.config(state=tk.DISABLED)
            messagebox.showinfo("提示",
                                "你取消了自动下载。\n请务必手动将 ffmpeg.exe, fpcalc.exe 和 models 文件夹放置在软件同级目录下，然后重启本软件。")

    def show_download_dialog(self, download_urls, extract_to_path):
        """显示带有进度条的下载界面"""
        dl_win = Toplevel(self.root)
        dl_win.title("正在下载核心依赖环境")
        dl_win.geometry("450x180")
        dl_win.transient(self.root)
        dl_win.grab_set()  # 锁定主窗口，防止用户在下载时乱点

        # 居中显示
        dl_win.geometry(
            f"+{self.root.winfo_x() + self.root.winfo_width() // 2 - 225}+{self.root.winfo_y() + self.root.winfo_height() // 2 - 90}")

        tk.Label(dl_win, text="🚀 正在从 GitHub 下载运行库，请耐心等待...", font=("Microsoft YaHei", 10, "bold")).pack(
            pady=15)

        # 进度条
        progress_var = tk.DoubleVar()
        progress_bar = ttk.Progressbar(dl_win, variable=progress_var, maximum=100, length=380)
        progress_bar.pack(pady=5)

        # 状态文本 (显示下载速度和百分比)
        status_label = tk.Label(dl_win, text="准备连接...", font=("Consolas", 9), fg="#555")
        status_label.pack(pady=5)

        # 启动后台下载线程
        threading.Thread(
            target=self.download_and_extract_worker,
            args=(download_urls, extract_to_path, dl_win, progress_var, status_label),
            daemon=True
        ).start()

    def download_and_extract_worker(self, download_urls, extract_to_path, window, progress_var, status_label):
        """后台执行流式下载与解压的工作线程"""
        try:
            for zip_name, url in download_urls:
                zip_path = os.path.join(extract_to_path, f"{zip_name}_temp.zip")
                
                # 初始化/重置该文件的 UI 状态
                self.root.after(0, lambda z=zip_name: status_label.config(text=f"准备连接并下载 {z}..."))
                self.root.after(0, lambda: progress_var.set(0))

                # 1. 发起请求并获取文件总大小 (流式下载)
                response = requests.get(url, stream=True, timeout=15)
                response.raise_for_status()

                total_size = int(response.headers.get('content-length', 0))
                block_size = 1024 * 64  # 64 KB 每次读取
                downloaded = 0

                # 2. 写入本地文件，并实时更新 GUI 进度
                with open(zip_path, 'wb') as file:
                    for data in response.iter_content(block_size):
                        if not data: break
                        file.write(data)
                        downloaded += len(data)

                        if total_size > 0:
                            percent = (downloaded / total_size) * 100
                            mb_downloaded = downloaded / (1024 * 1024)
                            mb_total = total_size / (1024 * 1024)

                            # 必须用 after 把 UI 更新推回主线程，防止跨线程操作 Tkinter 崩溃
                            # 使用默认参数绑定局部变量，防止 lambda 仅捕获到最后一次循环的值
                            self.root.after(0, lambda p=percent, d=mb_downloaded, t=mb_total, z=zip_name:[
                                progress_var.set(p),
                                status_label.config(text=f"下载 {z}: {d:.1f} MB / {t:.1f} MB ({p:.1f}%)")
                            ])

                # 3. 开始解压
                self.root.after(0, lambda z=zip_name: status_label.config(text=f"📦 {z} 下载完成，正在解压部署，请稍候..."))
                
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_to_path)

                # 4. 清理当前的临时压缩包
                if os.path.exists(zip_path):
                    os.remove(zip_path)

            # 5. 全部队列完成并关闭窗口
            self.root.after(0, window.destroy)
            self.root.after(0, lambda: messagebox.showinfo("部署成功", "🎉 核心依赖已全部部署完毕！请尽情使用。"))
            self.root.after(0, lambda: self.status_var.set("环境部署完成，系统就绪。"))

        except Exception as e:
            # 下载或解压出错时的处理：清理残余的临时压缩包
            for zip_name, _ in download_urls:
                zip_path = os.path.join(extract_to_path, f"{zip_name}_temp.zip")
                if os.path.exists(zip_path):
                    try:
                        os.remove(zip_path)
                    except:
                        pass

            error_msg = f"自动部署失败: {str(e)}\n\n可能由于网络不稳定或连接 GitHub 失败。\n建议尝试开启代理，或者手动前往项目主页下载依赖包。"
            self.root.after(0, window.destroy)
            self.root.after(0, lambda: messagebox.showerror("下载错误", error_msg))
            self.root.after(0, lambda: self.status_var.set("⚠️ 环境自动配置失败，需手动介入。"))


    def get_folder_size(self, folder_path):
        """获取文件夹的总大小"""
        total_size = 0
        for dirpath, _, filenames in os.walk(folder_path):  # 不需要 dirnames，直接丢弃
            for f in filenames:
                total_size += os.path.getsize(os.path.join(dirpath, f))
        return total_size
