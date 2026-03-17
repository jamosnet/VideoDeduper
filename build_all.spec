# -*- mode: python ; coding: utf-8 -*-

import os
import shutil
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ==========================================
# 👑 终极暴力破解：自动化你的手工复制操作！
# ==========================================
# 动态检测是 funasr_onnx 还是 funasr，适配最新的依赖变化
try:
    import funasr_onnx as asr_pkg
    asr_pkg_name = 'funasr_onnx'
except ImportError as e:
    print(f" 导入funasr_onnx具体的错误提示为: {e}")
    import funasr as asr_pkg
    asr_pkg_name = 'funasr'

asr_real_path = os.path.dirname(asr_pkg.__file__)
print(f"\n🔥 侦测到本机 ASR 真实路径: {asr_real_path} (包名: {asr_pkg_name})")
print("🔥 正在启用核弹级打包策略：全量物理拷贝...\n")

# 强制将整个 ASR 源码文件夹当作“静态文件”拷贝到打包后的 _internal 里
brute_force_datas = [ (asr_real_path, asr_pkg_name) ]

# 其他常规库的数据 (新增 onnxruntime)
other_datas = collect_data_files('modelscope') + collect_data_files('jieba') + collect_data_files('onnxruntime')
all_datas = brute_force_datas + other_datas
all_hidden_imports = collect_submodules('modelscope') + collect_submodules('onnxruntime')

# ==========================================
# 1. 定义所有的独立脚本 (Analysis)
# ==========================================
a_gui = Analysis(['main_gui.py'], pathex=[], hiddenimports=[], hookspath=[], runtime_hooks=[], datas=[('app.ico', '.')])
a_db = Analysis(['db_builder.py'], pathex=[], hiddenimports=[], hookspath=[], runtime_hooks=[])
a_audio = Analysis(['audio_cleaner.py'], pathex=[], hiddenimports=[], hookspath=[], runtime_hooks=[])
a_visual = Analysis(['visual_matcher.py'], pathex=[], hiddenimports=[], hookspath=[], runtime_hooks=[])
a_gui_manual = Analysis(['gui_manual_inspector.py'], pathex=[], hiddenimports=[], hookspath=[], runtime_hooks=[])
# ASR 处理器需要带上模型所需的所有隐藏依赖和数据
a_asr_processor = Analysis(['asr_processor.py'], pathex=[], hiddenimports=all_hidden_imports, hookspath=[], runtime_hooks=[], datas=all_datas)


# ==========================================
# 2. 合并它们的公共依赖 (MERGE 魔法)
# ==========================================
MERGE(
    (a_gui, 'main_gui', 'main_gui'),
    (a_db, 'db_builder', 'db_builder'),
    (a_audio, 'audio_cleaner', 'audio_cleaner'),
    (a_visual, 'visual_matcher', 'visual_matcher'),
    (a_gui_manual, 'gui_manual_inspector', 'gui_manual_inspector'),
    (a_asr_processor, 'asr_processor', 'asr_processor')
)

# ==========================================
# 3. 分别生成各自的 PYZ 和 EXE
# ==========================================
# 主控 GUI (console=False 隐藏黑框，这里为了调试如果需要可改为True)
pyz_gui = PYZ(a_gui.pure)
exe_gui = EXE(pyz_gui, a_gui.scripts, exclude_binaries=True, name='main_gui', console=True, icon='app.ico')

# 建库后台进程
pyz_db = PYZ(a_db.pure)
exe_db = EXE(pyz_db, a_db.scripts, exclude_binaries=True, name='db_builder', console=True)

# 音频后台进程
pyz_audio = PYZ(a_audio.pure)
exe_audio = EXE(pyz_audio, a_audio.scripts, exclude_binaries=True, name='audio_cleaner', console=True)

# 视觉后台进程
pyz_visual = PYZ(a_visual.pure)
exe_visual = EXE(pyz_visual, a_visual.scripts, exclude_binaries=True, name='visual_matcher', console=True)

# 视觉人工进程
pyz_gui_manual = PYZ(a_gui_manual.pure)
exe_gui_manual = EXE(pyz_gui_manual, a_gui_manual.scripts, exclude_binaries=True, name='gui_manual_inspector', console=True)

# ASR后台进程
pyz_asr_processor = PYZ(a_asr_processor.pure)
exe_asr_processor = EXE(pyz_asr_processor, a_asr_processor.scripts, exclude_binaries=True, name='asr_processor', console=True)


# ==========================================
# 4. 统一收集，输出到一个文件夹中 (COLLECT)
# ==========================================
coll = COLLECT(
    exe_gui, a_gui.binaries, a_gui.datas,
    exe_db, a_db.binaries, a_db.datas,
    exe_audio, a_audio.binaries, a_audio.datas,
    exe_visual, a_visual.binaries, a_visual.datas,
    exe_gui_manual, a_gui_manual.binaries, a_gui_manual.datas,
    exe_asr_processor, a_asr_processor.binaries, a_asr_processor.datas,
    strip=False,
    upx=True,
    name='VideoDedupeTool'  # 最终生成的文件夹名称
)


# ==========================================
# 5. 终极必杀：强制把外部工具拷贝到根目录
# ==========================================
# 定义目标输出根目录 (跟你的 COLLECT name 保持一致)
dist_root = os.path.join('dist', 'VideoDedupeTool')

# 只要打包一结束，我们自己动手把它们复制到外面去！
tools = ['ffmpeg.exe', 'ffprobe.exe', 'fpcalc.exe']
for tool in tools:
    if os.path.exists(tool):
        print(f"✅ 正在强制拷贝 {tool} 到根目录...")
        shutil.copy2(tool, os.path.join(dist_root, tool))
    else:
        print(f"⚠️ 警告: 找不到源文件 {tool}，请确保它在打包目录下。")