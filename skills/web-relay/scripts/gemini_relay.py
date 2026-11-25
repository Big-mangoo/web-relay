#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
gemini_relay.py   网页端大模型与本地智能体 人在回路问答接力脚本   v1.0.0
================================================================================
【程序功能】
  把本地项目文件 + 任务说明打包成"契约化提示词"并写入系统剪贴板 -> 用户粘贴到
  网页端大模型 -> 复制模型回复 -> 脚本解析 <file>/<exec>/<status>/<summary> 标签
  -> 安全落盘 -> 自动执行验证命令 -> 失败或未完成时自动把下一轮提示词重新写入
  剪贴板，形成"发球—接球—自动续球"的多轮人机接力闭环。

【功能点列表】
  1. pack   ：读取文件/目录、目录树概览、token 体量估算、组装提示词并写剪贴板
              （--out 可同时落盘为文本，--stdout 可打印全文）
  2. 输出契约：<file path="相对路径">完整文件</file> / <exec>命令</exec> /
              <status>continue|done</status> / <summary>一句话</summary>，
              系统说明 + 尾部双重加固，命令模型不得输出散装代码块。
  3. catch  ：读剪贴板（或 --input 回复文件）-> 状态机解析 -> 展示变更计划与
              差异预览 -> 人工确认 -> 原子落盘（临时文件校验后 os.replace，
              自动创建父目录）-> 逐条执行 <exec> 并实时回显终端输出。
  4. 安全门禁：绝对路径/.. 越级/盘符/冒号替代流拦截；rm -rf、del /f /s、format、
              diskpart、shutdown、git reset --hard 等高危命令黑名单；落地前必须确认。
  5. relay  ：pack 后直接进入 catch 多轮循环；报错或 status=continue 时自动续球，
              把"失败命令 + 报错输出 + 修复要求 + 相关文件最新内容"重新打包进剪贴板。
  6. 剪贴板 ：pyperclip -> Windows PowerShell(Get/Set-Clipboard) -> ctypes Win32
              三级无缝兜底；macOS 走 pbcopy/pbpaste；Linux 走 xclip/xsel/wl-clipboard。
  7. selftest：内置回归自测（解析/路径安全/危险命令/提示词/token 估算/原子落盘/
              续球提示词），一条命令自证核心逻辑。

【本次 v1.0.0 新增/修改/移除】
  + 首版：pack / catch / relay / selftest 四个子命令与完整多轮接力闭环
  + 三级剪贴板后端与 ANSI 彩色终端指引（Windows 自动启用 VT 转义）
  + 文件路径安全校验、危险命令黑名单、原子写入、非交互环境保护

【核心依赖】
  Python >= 3.8（仅标准库：argparse re subprocess difflib tempfile pathlib
                threading sys os time）
  可选：pyperclip（未安装时自动走 PowerShell 或 ctypes，无需任何第三方包）

【用法示例】
  python gemini_relay.py pack -f main.py utils.py -p "修复计算精度问题"
  python gemini_relay.py pack -f src/ -p "给所有函数补类型注解" --out prompt.txt
  python gemini_relay.py catch                 # 读剪贴板，落地 + 执行 + 自动续球
  python gemini_relay.py catch --once -y        # 只做一轮，自动确认
  python gemini_relay.py relay -f . -p "扫描并修复所有 lint 问题"
  python gemini_relay.py selftest
================================================================================
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

VERSION = "1.1.0"
DEFAULT_TIMEOUT = 600.0        # 单条 <exec> 命令的超时秒数
OUTPUT_TAIL_CHARS = 3000       # 回灌给模型的报错输出尾部长度
CONTEXT_FILE_LIMIT = 60_000    # 续球提示词中单文件最大字节数
CONTEXT_TOTAL_LIMIT = 200_000  # 续球提示词中文件内容总字节上限
ARCHIVE_DIR = ".web-relay"     # 归档根目录（不进快照、不接受模型写入）

# 疑似凭证文件：默认跳过，--include-secrets 或显式单文件时放行
SENSITIVE_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "id_rsa*",
                      "id_ed25519*", "*credentials*.json")
SENSITIVE_ALLOW = (".env.example", ".env.sample", ".env.template")

SKIP_DIRS = {
    ".git", ".svn", ".hg", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "node_modules", ".venv", "venv", "env", ".idea", ".vscode",
    "_scratch", "dist", "build", "target", ".next", ".gradle", ".tox",
    ARCHIVE_DIR,
}

# ============================================================================
# 一、终端彩色输出
# ============================================================================
_ANSI = {
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "cyan": "\033[36m", "bold": "\033[1m", "dim": "\033[2m", "reset": "\033[0m",
}
_COLOR_ON = False


def _setup_console() -> None:
    """Windows 控制台统一 UTF-8，避免中文/emoji 编码异常。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _enable_vt() -> bool:
    """在 Windows 控制台启用 ANSI 转义（VT）处理。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _init_color() -> None:
    global _COLOR_ON
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        _COLOR_ON = False
        return
    _COLOR_ON = _enable_vt()


def c(text, color: str) -> str:
    """着色辅助：非交互或 NO_COLOR 时自动退化为纯文本。"""
    if not _COLOR_ON:
        return str(text)
    return f"{_ANSI.get(color, '')}{text}{_ANSI['reset']}"


# ============================================================================
# 二、剪贴板（pyperclip -> PowerShell -> ctypes，三级兜底）
# ============================================================================
def _pyperclip_get() -> str:
    import pyperclip  # 可选依赖；未安装时抛 ImportError 由上层跳过
    return pyperclip.paste()


def _pyperclip_set(text: str) -> None:
    import pyperclip
    pyperclip.copy(text)


def _ps_run(command: str, env_extra=None, timeout: float = 20.0):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env,
    )


def _ps_get() -> str:
    cmd = ("try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}; "
           "$t = Get-Clipboard -Raw; "
           "if ($null -eq $t) { '' } else { $t }")
    res = _ps_run(cmd)
    if res.returncode != 0:
        raise RuntimeError(f"Get-Clipboard 失败: {res.stderr.strip()[:200]}")
    return res.stdout


def _ps_set(text: str) -> None:
    fd, tmp_path = tempfile.mkstemp(prefix="relay_clip_", suffix=".txt")
    os.close(fd)
    try:
        Path(tmp_path).write_text(text, encoding="utf-8")
        cmd = ("Set-Clipboard -Value "
               "(Get-Content -Raw -Encoding UTF8 -LiteralPath $env:RELAY_CLIP_FILE)")
        res = _ps_run(cmd, {"RELAY_CLIP_FILE": tmp_path})
        if res.returncode != 0:
            raise RuntimeError(f"Set-Clipboard 失败: {res.stderr.strip()[:200]}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _win32_get() -> str:
    import ctypes
    cf_unicode = 13
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.GetClipboardData.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not u32.OpenClipboard(None):
        raise RuntimeError("OpenClipboard 失败")
    try:
        handle = u32.GetClipboardData(cf_unicode)
        if not handle:
            return ""
        ptr = k32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            k32.GlobalUnlock(handle)
    finally:
        u32.CloseClipboard()


def _win32_set(text: str) -> None:
    import ctypes
    cf_unicode, gmem_moveable = 13, 0x0002
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    for _ in range(10):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("OpenClipboard 被占用")
    try:
        u32.EmptyClipboard()
        raw = text.encode("utf-16-le") + b"\x00\x00"
        handle = k32.GlobalAlloc(gmem_moveable, len(raw))
        if not handle:
            raise RuntimeError("GlobalAlloc 失败")
        ptr = k32.GlobalLock(handle)
        if not ptr:
            raise RuntimeError("GlobalLock 失败")
        try:
            ctypes.memmove(ptr, raw, len(raw))
        finally:
            k32.GlobalUnlock(handle)
        if not u32.SetClipboardData(cf_unicode, handle):
            raise RuntimeError("SetClipboardData 失败")
    finally:
        u32.CloseClipboard()


def _pbcopy_get() -> str:
    return subprocess.run(["pbpaste"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=10).stdout


def _pbcopy_set(text: str) -> None:
    subprocess.run(["pbcopy"], input=text, text=True, encoding="utf-8", timeout=10, check=True)


def _xclip_get() -> str:
    for cmd in (["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"],
                ["wl-paste"]):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=10)
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        if res.returncode == 0:
            return res.stdout
    raise RuntimeError("没有可用的 Linux 剪贴板工具")


def _xclip_set(text: str) -> None:
    for cmd in (["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
                ["wl-copy"]):
        try:
            res = subprocess.run(cmd, input=text, text=True, encoding="utf-8",
                                 timeout=10)
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        if res.returncode == 0:
            return
    raise RuntimeError("没有可用的 Linux 剪贴板工具")


def _clip_backends():
    """返回 (名称, 读函数, 写函数) 候选列表，按优先级排列。"""
    if os.name == "nt":
        return [("pyperclip", _pyperclip_get, _pyperclip_set),
                ("powershell", _ps_get, _ps_set),
                ("win32-ctypes", _win32_get, _win32_set)]
    if sys.platform == "darwin":
        return [("pyperclip", _pyperclip_get, _pyperclip_set),
                ("pbpaste", _pbcopy_get, _pbcopy_set)]
    return [("pyperclip", _pyperclip_get, _pyperclip_set),
            ("xclip", _xclip_get, _xclip_set)]


def clip_get() -> str:
    errors = []
    for name, getter, _ in _clip_backends():
        try:
            text = getter()
        except Exception as exc:                     # 后端不可用则降级
            errors.append(f"{name}: {type(exc).__name__}")
            continue
        if text is not None:
            return str(text)
    raise RuntimeError("剪贴板读取失败，所有后端均不可用 -> " + "; ".join(errors))


def clip_set(text: str) -> str:
    errors = []
    for name, _, setter in _clip_backends():
        try:
            setter(text)
            return name
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    raise RuntimeError("剪贴板写入失败，所有后端均不可用 -> " + "; ".join(errors))


# ============================================================================
# 三、通用 I/O 辅助
# ============================================================================
def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：CJK 约 1 字/token，其余约 3.6 字符/token。"""
    if not text:
        return 0
    cjk = 0
    for ch in text:
        code = ord(ch)
        if (0x4E00 <= code <= 0x9FFF or 0x3000 <= code <= 0x303F
                or 0xFF00 <= code <= 0xFFEF or 0x3400 <= code <= 0x4DBF):
            cjk += 1
    return int(cjk + (len(text) - cjk) / 3.6)


def read_text_file(path: Path):
    """按 UTF-8/GBK 顺序解码；二进制返回 (None, None)。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    if b"\x00" in data[:4096]:
        return None, None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return None, None


def _atomic_write(target: Path, content: str) -> None:
    """先写同目录临时文件再原子替换，避免半截文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent),
                               prefix=f".{target.name}.", suffix=".relaytmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def safe_target(root: Path, rel: str):
    """把模型给出的相对路径解析到项目根内；越界/绝对路径一律拒绝。"""
    rel = (rel or "").strip().strip('"').strip("'").replace("\\", "/")
    if not rel:
        return None, "路径为空"
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return None, "禁止绝对路径"
    if ":" in rel:
        return None, "禁止冒号（NTFS 备用数据流）"
    root_res = root.resolve()
    try:
        target = (root_res / rel).resolve()
    except (OSError, ValueError):
        return None, "路径非法"
    try:
        parts = target.relative_to(root_res).parts
    except ValueError:
        return None, "路径越出项目根（..）"
    if parts and parts[0].lower() == ARCHIVE_DIR:
        return None, "禁止写入归档目录"
    if target == root_res:
        return None, "路径指向项目根目录本身"
    return target, None


def rel_display(target: Path, root: Path) -> str:
    try:
        return target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return target.as_posix()


def _utc_stamp() -> str:
    """UTC 毫秒时间戳；单次采样，避免跨秒拆样导致前缀与毫秒错位。"""
    ns = time.time_ns()
    return (time.strftime("%Y%m%dT%H%M%S", time.gmtime(ns // 1_000_000_000))
            + f"{ns // 1_000_000 % 1000:03d}Z")


def _project_slug(root: Path) -> str:
    return re.sub(r"[^\w.-]+", "_", root.name)[:40] or "proj"


def _archive_root(root: Path) -> Path:
    return root.resolve() / ARCHIVE_DIR / _project_slug(root)


def next_round(archive: Path, direction: str) -> int:
    """out 取 max+1；in 复用尚未配对的最大轮次，否则 max+1。"""
    rounds, in_rounds = [], set()
    if archive.is_dir():
        for path in archive.glob("r*-*.md"):
            m = re.match(r"r(\d+)-(out|in)-", path.name)
            if not m:
                continue
            num = int(m.group(1))
            rounds.append(num)
            if m.group(2) == "in":
                in_rounds.add(num)
    if not rounds:
        return 1
    max_r = max(rounds)
    if direction == "out":
        return max_r + 1
    return max_r if max_r not in in_rounds else max_r + 1


def archive_put(root: Path, direction: str, text: str,
                round_no=None) -> Path:
    """尽力而为地归档；失败只告警，绝不阻断发球/接球。"""
    archive = _archive_root(root)
    try:
        archive.mkdir(parents=True, exist_ok=True)
        num = round_no if round_no is not None else next_round(archive, direction)
        path = archive / f"r{num:03d}-{direction}-{_utc_stamp()}.md"
        _atomic_write(path, text)
        return path
    except OSError as exc:
        print(c(f"  [归档失败·不阻断] {exc}", "yellow"))
        return None


def _norm_for_hash(text: str) -> str:
    """剪贴板往返会改变行尾与尾部空白，归一化后再比对。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip().lstrip("﻿")


def _write_outbound_hash(root: Path, text: str) -> None:
    digest = hashlib.sha256(_norm_for_hash(text).encode("utf-8")).hexdigest()
    try:
        _atomic_write(_archive_root(root).parent / "last_outbound.sha256",
                      digest + "\n")
    except OSError as exc:
        print(c(f"  [哈希记录失败·不阻断] {exc}", "yellow"))


def _is_echo_of_last_outbound(root: Path, text: str) -> bool:
    try:
        recorded = (_archive_root(root).parent / "last_outbound.sha256").read_text(
            encoding="utf-8").strip()
    except OSError:
        return False
    if not recorded:
        return False
    return hashlib.sha256(_norm_for_hash(text).encode("utf-8")).hexdigest() == recorded


def _is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    if name in SENSITIVE_ALLOW:
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE_PATTERNS)


def diff_stat(old_text, new_text):
    added = removed = 0
    for line in difflib.unified_diff((old_text or "").splitlines(),
                                     (new_text or "").splitlines(),
                                     lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def build_tree(root: Path, max_depth: int = 3, max_entries: int = 200) -> str:
    lines = []
    truncated = {"hit": False}

    def walk(directory: Path, prefix: str, depth: int) -> None:
        if depth > max_depth or truncated["hit"]:
            return
        try:
            entries = sorted(directory.iterdir(),
                             key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        entries = [e for e in entries if e.name not in SKIP_DIRS]
        for entry in entries:
            if len(lines) >= max_entries:
                lines.append(f"{prefix}... （条目过多，已截断）")
                truncated["hit"] = True
                return
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                lines.append(f"{prefix}{entry.name}/")
                walk(entry, prefix + "  ", depth + 1)
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{prefix}{entry.name}  ({human_size(size)})")

    walk(root, "", 1)
    return "\n".join(lines) if lines else "（空目录）"


def collect_files(raw_paths, root: Path, max_bytes: int, max_files: int,
                  include_secrets: bool = False):
    """展开用户给定的文件/目录为 [{rel, text, size}]，跳过二进制与超大文件。"""
    root_res = root.resolve()
    entries, seen, notes = [], set(), []
    for raw in raw_paths:
        raw = str(raw).strip().strip('"').strip("'")
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            notes.append(f"跳过不存在的路径：{raw}")
            continue
        explicit_file = path.is_file()
        if explicit_file:
            candidates = [path]
        else:
            try:
                # 只在「相对本次遍历根」的路径段上套 SKIP_DIRS：
                # 项目本身若位于 _scratch/build 之类目录下，不该整盘归零
                base = path.resolve()
                candidates = [p for p in sorted(path.rglob("*"))
                              if p.is_file()
                              and not any(part in SKIP_DIRS
                                          for part in p.relative_to(base).parts)]
            except OSError as exc:
                notes.append(f"无法遍历 {raw}：{exc}")
                continue
            if not candidates:
                notes.append(f"目录 {raw} 下没有可打包的文件（检查是否被忽略规则滤空）")
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            if not include_secrets and not explicit_file and _is_sensitive(resolved):
                notes.append(f"跳过疑似凭证文件 {resolved.name}"
                             "（显式指定该文件或加 --include-secrets 可强制打包）")
                continue
            seen.add(resolved)
            if len(seen) > max_files:
                notes.append(f"文件数已达上限 {max_files}，其余未打包")
                break
            try:
                size = resolved.stat().st_size
            except OSError:
                continue
            if size > max_bytes:
                notes.append(f"跳过超大文件 {resolved.name}"
                             f"（{human_size(size)} > {human_size(max_bytes)}）")
                continue
            text, _enc = read_text_file(resolved)
            if text is None:
                notes.append(f"跳过二进制/无法解码文件 {resolved.name}")
                continue
            try:
                rel = resolved.relative_to(root_res).as_posix()
            except ValueError:
                try:
                    rel = os.path.relpath(resolved, root_res).replace("\\", "/")
                except ValueError:
                    rel = resolved.as_posix()      # 跨盘符：无法相对化，保留绝对路径
                notes.append(f"注意：{rel} 位于项目根之外")
            entries.append({"rel": rel, "text": text.rstrip("\n"), "size": size})
        if len(seen) > max_files:
            break
    return entries, notes


# ============================================================================
# 四、提示词契约（头部 + 尾部双重加固）
# ============================================================================
CONTRACT_HEAD = """\
你是一名严谨的软件工程助手。下面给出本地项目快照与任务说明，请直接产出可落盘的完整修改方案。

【输出格式契约 · 必须严格遵守】
1) 每个被修改或新建的文件，用一个标签输出**完整文件内容**（禁止省略、禁止写"其余不变"）：
<file path="相对路径/文件名">完整文件内容</file>
2) 建议在本地执行的验证命令（0~3 条，一条一个标签）：
<exec>命令行</exec>
3) 任务是否结束：
<status>continue</status> 或 <status>done</status>
4) 一句话总结：
<summary>改了什么</summary>

【硬性规则】
- 回复中只允许出现上述四种标签，禁止 Markdown 代码围栏、禁止寒暄、禁止散装代码块。
- <file> 内必须是可直接覆盖写入的完整文件，一个标签只放一个文件。
- path 必须是相对路径，禁止绝对路径，禁止 .. 越级。
- 未修改的文件不要输出；无需修改时只输出 <exec> 与 <status>。
- 修改后代码必须能直接运行；新建文件所需父目录会自动创建。
- 若任务需多轮迭代，先把本轮能完成的部分输出，并将 <status> 置为 continue。
- <exec> 中的命令必须是本地终端可直接执行的**非交互式**命令：
  Windows 走 cmd.exe、macOS/Linux 走 bash/sh，因此避免 Get-ChildItem、
  $env: 之类 PowerShell 专属写法，也不要启动需要人工输入的程序。
- 只做任务明确要求的最小改动，避免擅自添加未被要求的防御性补丁、复杂兜底状态机或无关重构。
- 使用与用户任务相同的语言书写 summary。"""

CONTRACT_TAIL = """\
【再次强调输出契约 · 本地脚本将按此自动解析】
本次回复只能包含 <file path="...">...</file>、<exec>...</exec>、
<status>continue|done</status>、<summary>...</summary> 四类标签；
不要使用 ``` 代码围栏，不要输出任何标签之外的说明文字。"""


def build_pack_prompt(root: Path, entries, task: str, tree: str, extra: str = "") -> str:
    blocks = [f'<file path="{e["rel"]}">\n{e["text"]}\n</file>' for e in entries]
    file_tokens = estimate_tokens("\n\n".join(blocks))
    parts = [CONTRACT_HEAD]
    parts.append("## 一、任务\n" + (task.strip() or "（未提供任务描述，请先指出需要处理的问题）"))
    parts.append("## 二、目录树概览（已忽略 .git/__pycache__/_scratch 等）\n```\n"
                 + tree + "\n```")
    if blocks:
        parts.append(f"## 三、相关文件快照（共 {len(entries)} 个，约 {file_tokens} tokens）\n"
                     + "\n\n".join(blocks))
    if extra.strip():
        parts.append("## 四、补充说明\n" + extra.strip())
    parts.append(CONTRACT_TAIL)
    return "\n\n".join(parts)


def build_next_prompt(root: Path, round_no: int, applied, failed_apply,
                      exec_reports, summary: str, status: str,
                      include_files: bool = True) -> str:
    """报错/未完成时组装下一轮接力提示词。"""
    problems = [r for r in exec_reports if r.get("blocked") or r.get("rc") != 0]
    if problems:
        target = "上述修改在执行命令时报错如下，请诊断并输出修复补丁（仍是完整文件，不要只给 diff 片段）。"
    elif failed_apply:
        target = "上一轮存在未落盘的文件（路径非法或内容为空），请重新输出这些文件的完整内容。"
    elif status == "continue":
        target = "上一轮修改已落地（如无报错则视为通过基础检查），请继续输出下一步修改。"
    else:
        target = "请复核上一轮结果并输出本轮需要的完整修改。"

    lines = [f"- 轮次：第 {round_no} 轮",
             f"- 上一轮状态：{status}",
             f"- 上一轮总结：{summary or '（未提供）'}"]
    if applied:
        lines.append("- 已落盘文件：" + "、".join(applied))
    if failed_apply:
        lines.append("- 未落盘文件：" + "、".join(failed_apply))
    if exec_reports:
        lines.append("- 验证命令结果：")
        for rep in exec_reports:
            if rep.get("blocked"):
                tag = "被安全门禁拦截"
            elif rep.get("rc") == 0:
                tag = "通过"
            elif rep.get("timed_out"):
                tag = "超时"
            else:
                tag = f"失败 rc={rep.get('rc')}"
            lines.append(f"  * {rep['cmd']}  ->  {tag}")

    parts = [CONTRACT_HEAD, "## 一、上一轮接力结果\n" + "\n".join(lines),
             "## 二、本轮要求\n" + target]

    for rep in problems:
        output = (rep.get("output") or "").strip()
        if rep.get("blocked"):
            output = f"该命令被本地安全门禁拦截：{rep.get('reason', '命中危险命令特征')}"
        if not output:
            continue
        tail = output[-OUTPUT_TAIL_CHARS:]
        parts.append(f"### 命令 {rep['cmd']} 的输出（尾部截断）\n```\n{tail}\n```")

    if include_files and applied:
        blocks, total = [], 0
        for rel in applied:
            target_path, err = safe_target(root, rel)
            if target_path is None or not target_path.exists():
                continue
            text, _ = read_text_file(target_path)
            if text is None:
                continue
            raw = len(text.encode("utf-8"))
            if raw > CONTEXT_FILE_LIMIT or total + raw > CONTEXT_TOTAL_LIMIT:
                parts.append(f"（文件 {rel} 过大，未附全文，请基于报错信息定位问题）")
                continue
            total += raw
            blocks.append(f'<file path="{rel}">\n{text.rstrip(chr(10))}\n</file>')
        if blocks:
            parts.append("## 三、相关文件当前内容（以磁盘最新为准）\n" + "\n\n".join(blocks))

    parts.append(CONTRACT_TAIL)
    return "\n\n".join(parts)


# ============================================================================
# 五、回复解析（状态机式正则，容忍围栏与松散空白）
# ============================================================================
FILE_OPEN_RE = re.compile(
    r"<file\s+path\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>\"']+))\s*>", re.I)
FILE_CLOSE_RE = re.compile(r"</file\s*>", re.I)
EXEC_RE = re.compile(r"<exec(?:\s[^>]*)?>(.*?)</exec\s*>", re.I | re.S)
STATUS_RE = re.compile(r"<status\s*>(.*?)</status\s*>", re.I | re.S)
SUMMARY_RE = re.compile(r"<summary\s*>(.*?)</summary\s*>", re.I | re.S)


def _find_file_close(text: str, start: int, limit: int):
    """优先选择"闭合标签之后紧跟另一个标签或文末"的位置，降低正文误命中。"""
    pos = start
    while True:
        match = FILE_CLOSE_RE.search(text, pos, limit)
        if not match:
            return FILE_CLOSE_RE.search(text, start, limit)
        tail = text[match.end():]
        idx = 0
        while idx < len(tail) and tail[idx] in " \t\r\n":
            idx += 1
        if idx >= len(tail) or tail[idx] == "<":
            return match
        pos = match.end()


_INVISIBLE_MAP = {
    "\u00a0": " ",   # NBSP  -> 空格（删了会让 return x 变成 returnx）
    "\u202f": " ",   # NNBSP -> 空格
    "\u200b": "",    # 零宽空格 ZWSP
}
# 刻意不处理 U+200C / U+200D（会破坏复合 Emoji）、U+3000（中文正文合法）


def _clean_text(text: str, *, strip_bom: bool = False) -> str:
    if strip_bom:
        text = text.lstrip("\ufeff")   # BOM，仅剥首部
    for bad, good in _INVISIBLE_MAP.items():
        text = text.replace(bad, good)
    return text


def _clean_content(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\n"):
        text = text[1:]
    # 顺序不可颠倒：BOM 常紧跟开标签后的换行，须先剥换行再剥 BOM
    text = _clean_text(text, strip_bom=True)
    if not text.strip():
        return ""
    return text.rstrip("\n") + "\n"


def parse_reply(text: str) -> dict:
    """解析模型回复，返回 files / execs / status / summary。"""
    text = (text or "").replace("\r\n", "\n")
    opens = list(FILE_OPEN_RE.finditer(text))
    files, order, unclosed = {}, [], []
    spans = []          # <file> 正文区间：外层标签扫描必须跳过
    for idx, match in enumerate(opens):
        rel = match.group(1) or match.group(2) or match.group(3) or ""
        rel = rel.strip()
        limit = opens[idx + 1].start() if idx + 1 < len(opens) else len(text)
        close = _find_file_close(text, match.end(), limit)
        if close is None:
            unclosed.append(rel)
            spans.append((match.end(), limit))   # 未闭合块也整段屏蔽
            continue
        spans.append((match.end(), close.start()))
        content = _clean_content(text[match.end():close.start()])
        if not rel:
            continue
        if rel in files:
            # 同名重复输出：后者覆盖前者（模型常常先给片段后给全文）
            files[rel] = content
        else:
            files[rel] = content
            order.append(rel)

    # 用等长空白遮蔽 <file> 正文并保持偏移；否则正文里的
    # <exec>/<status>/<summary> 字面量会被当成真实标签执行
    masked_chars = list(text)
    for start, end in spans:
        for pos in range(start, min(end, len(masked_chars))):
            if masked_chars[pos] != "\n":
                masked_chars[pos] = " "
    masked = "".join(masked_chars)

    execs = []
    for match in EXEC_RE.finditer(masked):
        cmd = _clean_text(match.group(1).strip())
        if cmd:
            execs.append(cmd)

    status_match = STATUS_RE.findall(masked)
    status = "continue"
    raw_status = status_match[-1].strip().lower() if status_match else ""
    if raw_status in ("done", "complete", "completed", "finished", "finish"):
        status = "done"
    elif raw_status in ("continue", "next", "cont"):
        status = "continue"

    summary = ""
    sm = SUMMARY_RE.findall(masked)
    if sm:
        summary = re.sub(r"\s+", " ", sm[-1]).strip()

    return {
        "files": [(rel, files[rel]) for rel in order],
        "execs": execs,
        "status": status,
        "status_raw": raw_status,
        "summary": summary,
        "unclosed": unclosed,
    }


# ============================================================================
# 六、安全门禁
# ============================================================================
DANGEROUS_PATTERNS = [
    r"\brm\s+-[a-z]*[rf]", r"\brm\s+--recursive", r"\brmdir\s+/s", r"\brd\s+/s",
    r"\bdel\s+/[fsq]", r"\bdeltree\b", r"\bformat\s+[a-z]:", r"\bmkfs(\.\w+)?\b",
    r"\bdiskpart\b", r"\bshutdown\b", r"\breboot\b", r"\bpoweroff\b",
    r"\breg\s+delete\b", r"\bRemove-Item\b[^\n]*-(Recurse|Force)", r"\brm\s+-rf\b",
    r"\bdd\s+if=", r":\(\)\s*\{", r"\bcipher\s+/w", r">\s*/dev/sd",
    r"\bgit\s+reset\s+--hard\b", r"\bgit\s+clean\s+-[a-z]*[fd]",
    r"\bchmod\s+-R\s+777\s+/", r"\bchown\s+-R\b", r">\s*[A-Za-z]:\\\\?(Windows|系统)",
    r"\bsc\s+delete\b", r"\bnet\s+user\b", r"\bnetsh\b", r"\btaskkill\s+/f\s+/im",
    r"\bwmic\s+.*\bdelete\b", r"\battrib\s+-[rhs]", r"\bicacls\b[^\n]*/reset",
]
DANGEROUS_RE = re.compile("|".join(DANGEROUS_PATTERNS), re.I)


def danger_check(cmd: str) -> bool:
    return bool(DANGEROUS_RE.search(cmd or ""))


# ============================================================================
# 七、落盘计划与提交
# ============================================================================
def plan_files(files, root: Path):
    plan = []
    for rel, content in files:
        target, err = safe_target(root, rel)
        rec = {"path": rel, "target": target, "error": err, "old": None,
               "new": content, "added": 0, "removed": 0,
               "new_file": False, "unchanged": False, "ok": False}
        if target is None:
            plan.append(rec)
            continue
        if target.exists() and target.is_dir():
            rec["error"] = "目标已存在且是目录"
            plan.append(rec)
            continue
        if not content.strip():
            rec["error"] = "内容为空（模型未给出完整文件）"
            plan.append(rec)
            continue
        if target.exists():
            old, _enc = read_text_file(target)
            if old is None:
                rec["error"] = "既有文件无法按文本读取（疑似二进制）"
                plan.append(rec)
                continue
            rec["old"] = old
            if old.replace("\r\n", "\n") == content.replace("\r\n", "\n"):
                rec["unchanged"] = True
                rec["ok"] = True
                plan.append(rec)
                continue
        else:
            rec["new_file"] = True
        rec["added"], rec["removed"] = diff_stat(rec["old"], content)
        rec["ok"] = True
        plan.append(rec)
    return plan


def commit_plan(plan, root: Path, round_no: int = 0) -> None:
    root_res = root.resolve()
    # 同一轮的所有备份落在同一目录：时间戳必须在循环外只取一次
    batch = root_res / ARCHIVE_DIR / "backups" / f"{_utc_stamp()}-r{round_no:03d}"
    for rec in plan:
        if not rec["ok"] or rec.get("unchanged"):
            continue
        if rec["target"].exists():
            try:
                rel = rec["target"].relative_to(root_res)
                bak = batch / rel
                bak.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(rec["target"], bak)
            except OSError as exc:
                print(c(f"  [备份失败·不阻断] {rec['path']}: {exc}", "yellow"))
        _atomic_write(rec["target"], rec["new"])
        rec["written"] = True


def print_plan(plan, execs, summary: str, root: Path) -> None:
    print(c("\n将要落地的文件：", "bold"))
    any_ok = False
    for rec in plan:
        if not rec["ok"]:
            print(c(f"  [拒绝] {rec['path']}  <- {rec['error']}", "red"))
            continue
        any_ok = True
        if rec.get("unchanged"):
            print(f"  [无变化] {rel_display(rec['target'], root)}")
            continue
        tag = "新建" if rec["new_file"] else "修改"
        rel = rel_display(rec["target"], root)
        size = human_size(len(rec["new"].encode("utf-8")))
        print(f"  [{tag}] {rel}  {size}  " +
              c(f"(+{rec['added']} -{rec['removed']})", "green" if rec["added"] else "yellow"))
    if not plan and not execs:
        print(c("  （本轮没有可落盘的文件）", "yellow"))

    if execs:
        print(c("\n验证命令：", "bold"))
        for idx, cmd in enumerate(execs, 1):
            flag = c("  [危险命令·将拦截]", "red") if danger_check(cmd) else ""
            print(f"  {idx}. {cmd}{flag}")

    print(c("\n模型总结：", "bold") + (summary or "（未提供）"))


def print_diff_preview(plan, root: Path, max_lines: int = 40) -> None:
    budget = max_lines
    for rec in plan:
        if not rec["ok"] or rec.get("unchanged") or rec["old"] is None or budget <= 0:
            continue
        diff = list(difflib.unified_diff(
            rec["old"].splitlines(), rec["new"].splitlines(),
            fromfile=f"a/{rel_display(rec['target'], root)}",
            tofile=f"b/{rel_display(rec['target'], root)}", lineterm="", n=2))
        print(c(f"\n--- 差异预览 {rel_display(rec['target'], root)} ---", "dim"))
        for line in diff[:budget]:
            if line.startswith("+") and not line.startswith("+++"):
                print(c(line, "green"))
            elif line.startswith("-") and not line.startswith("---"):
                print(c(line, "red"))
            else:
                print(line)
        shown = min(len(diff), budget)
        budget -= shown
        if len(diff) > shown:
            print(c(f"  ...（其余 {len(diff) - shown} 行省略）", "dim"))


# ============================================================================
# 八、命令执行（实时回显 + 超时保护）
# ============================================================================
def run_command(cmd: str, cwd: Path, timeout: float) -> dict:
    print(c(f"\n  $ {cmd}", "cyan"))
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    killed = {"flag": False}
    proc = subprocess.Popen(
        cmd, shell=True, cwd=str(cwd), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env, bufsize=1)

    def _kill():
        killed["flag"] = True
        try:
            proc.kill()
        except OSError:
            pass

    timer = threading.Timer(timeout, _kill)
    timer.daemon = True
    timer.start()
    stored, dropped = [], 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            stored.append(line)
            if len(stored) > 5000:
                stored.pop(0)
                dropped += 1
        rc = proc.wait()
    finally:
        timer.cancel()
    if killed["flag"]:
        rc = -1
    output = "".join(stored)
    if dropped:
        output = f"...（省略前 {dropped} 行）\n" + output
    print(c(f"  -> returncode={rc}" +
            ("（超时被终止）" if killed["flag"] else ""),
            "green" if rc == 0 else "red"))
    return {"cmd": cmd, "rc": rc, "output": output, "timed_out": killed["flag"]}


# ============================================================================
# 九、子命令
# ============================================================================
class NoStdinError(RuntimeError):
    """非交互环境缺少人工确认入口。"""


def ask_yes(message: str, auto: bool = False) -> bool:
    if auto:
        return True
    if not sys.stdin.isatty():
        raise NoStdinError("非交互环境无法人工确认；请显式加 -y 以自动确认。")
    print(message, end="", flush=True)
    try:
        answer = input().strip().lower()
    except EOFError:
        raise NoStdinError("标准输入已结束，无法人工确认；请显式加 -y。")
    return answer in ("", "y", "yes", "是")


def wait_enter(message: str) -> bool:
    """返回 False 表示用户主动退出。非交互环境直接放行。"""
    if not sys.stdin.isatty():
        print(c(message, "yellow"))
        return True
    print(c(message, "yellow"), end="", flush=True)
    try:
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer not in ("q", "quit", "exit")


def _ask_text(message: str) -> str:
    try:
        return input(c(message, "cyan")).strip()
    except EOFError:
        return ""


def _interactive_inputs():
    raw = _ask_text("请输入要交给网页端大模型的文件或目录（空格分隔，回车跳过）：")
    return [chunk for chunk in re.split(r"[\s,;]+", raw) if chunk] if raw else []


def cmd_pack(args) -> int:
    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(c(f"项目根目录不存在：{root}", "red"))
        return 2
    root = root.resolve()

    raw_files = args.files
    if not raw_files:
        raw_files = _interactive_inputs()
    task = args.prompt
    if not task:
        task = _ask_text("请描述任务给网页端大模型：")

    entries, notes = collect_files(raw_files or [], root,
                                   args.max_file_bytes, args.max_files,
                                   getattr(args, "include_secrets", False))
    tree = build_tree(root)
    prompt = build_pack_prompt(root, entries, task or "", tree, args.extra or "")
    tokens = estimate_tokens(prompt)

    print(c("\n[打包结果]", "bold"))
    print(f"  项目根：{root}")
    print(f"  文件数：{len(entries)}    字符数：{len(prompt)}    估算 tokens：~{tokens}")
    for entry in entries:
        print(f"    - {entry['rel']}  ({human_size(entry['size'])})")
    for note in notes:
        print(c(f"  提示：{note}", "yellow"))
    if tokens > args.warn_tokens:
        print(c(f"  警告：估算 {tokens} tokens 已超过阈值 {args.warn_tokens}，"
                "网页端可能截断，建议减少文件或先拆分任务。", "red"))

    archive_path = archive_put(root, "out", prompt)

    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(out_path, prompt)
        print(f"  提示词已落盘：{out_path.resolve()}")
    if args.stdout:
        print("\n" + "=" * 70 + "\n" + prompt + "\n" + "=" * 70)

    try:
        backend = clip_set(prompt)
        _write_outbound_hash(root, prompt)
        print(c("\n🏓 发球完成！", "green", ) + f" 提示词已复制到剪贴板（后端：{backend}）。")
        if archive_path:
            print("   归档： " + c(str(archive_path), "dim"))
        print("   请到网页端按 " + c("Ctrl+V", "bold") + " 粘贴发送。")
        print("   拿到回复后，在回复上 " + c("Ctrl+A / Ctrl+C", "bold") + " 全选复制，然后运行：")
        print("   " + c("python gemini_relay.py catch", "bold"))
    except Exception as exc:
        print(c(f"\n剪贴板写入失败：{exc}", "red"))
        if archive_path:
            print("   提示词已存至归档，可手动打开复制：" + c(str(archive_path), "bold"))
        else:
            print("   请改用 --out prompt.txt 落盘后手动复制。")
        return 1
    return 0


def _decide_and_next(root: Path, reply, round_no: int, args) -> int:
    """一轮完整处理：确认 -> 落盘 -> 执行 -> 判定/续球。返回退出码或 -1 表示继续。"""
    plan = plan_files(reply["files"], root)
    print_plan(plan, reply["execs"], reply["summary"], root)
    if reply["unclosed"]:
        print(c(f"  警告：以下 <file> 缺少闭合标签，已忽略：{', '.join(reply['unclosed'])}", "yellow"))
    if reply["status_raw"] == "":
        print(c("  警告：未发现 <status> 标签，按 continue 处理。", "yellow"))
    print_diff_preview(plan, root)

    approved = False
    try:
        approved = ask_yes(c("\n确认按上述计划落盘？[Y/n] ", "bold"), auto=args.yes)
    except NoStdinError as exc:
        print(c(f"\n{exc}", "red"))
        return 2
    if not approved:
        print(c("已取消落盘，本轮未做任何改动。", "yellow"))
        return -2

    commit_plan(plan, root, round_no)
    applied = [rel_display(rec["target"], root) for rec in plan
               if rec["ok"] and not rec.get("unchanged") and rec.get("written")]
    failed_apply = [rec["path"] for rec in plan if not rec["ok"]]
    for rel in applied:
        print(c(f"  已写入 {rel}", "green"))

    exec_reports = []
    if args.no_exec:
        print(c("  （--no-exec：跳过命令执行）", "yellow"))
    else:
        for cmd in reply["execs"]:
            blocked = danger_check(cmd)
            if blocked and not args.allow_dangerous:
                print(c(f"\n  [安全门禁] 拦截高危命令：{cmd}", "red"))
                exec_reports.append({"cmd": cmd, "rc": -99, "output": "",
                                     "blocked": True,
                                     "reason": "命中危险命令黑名单（rm -rf / del /s / format 等）"})
                continue
            if blocked and args.allow_dangerous:
                try:
                    if not ask_yes(c(f"  该命令命中危险特征，仍要执行？[y/N] {cmd}\n> ", "red")):
                        exec_reports.append({"cmd": cmd, "rc": -99, "output": "",
                                             "blocked": True, "reason": "用户拒绝执行"})
                        continue
                except NoStdinError:
                    exec_reports.append({"cmd": cmd, "rc": -99, "output": "",
                                         "blocked": True,
                                         "reason": "非交互环境拒绝执行危险命令"})
                    continue
            exec_reports.append(run_command(cmd, root, args.timeout))

    apply_ok = all(rec["ok"] for rec in plan) if plan else True
    exec_failed = [rep for rep in exec_reports if rep.get("blocked") or rep.get("rc") != 0]
    success = apply_ok and not exec_failed

    if reply["status"] == "done" and success:
        print(c("\n🎉 任务完成！", "green"))
        if reply["summary"]:
            print("   总结：" + reply["summary"])
        if applied:
            print("   本轮落盘：" + "、".join(applied))
        return 0

    if reply["status"] == "done" and not success:
        print(c("\n任务标记 done，但存在失败项，转入自动续球。", "yellow"))
    next_prompt = build_next_prompt(
        root, round_no, applied, failed_apply, exec_reports,
        reply["summary"], reply["status"])
    # 先归档再写剪贴板：剪贴板失败时提示词仍在磁盘上
    archive_path = archive_put(root, "out", next_prompt)

    try:
        backend = clip_set(next_prompt)
        _write_outbound_hash(root, next_prompt)
    except Exception as exc:
        print(c(f"\n下一轮提示词写剪贴板失败：{exc}", "red"))
        if archive_path:
            print("   提示词已存至归档，可手动打开复制：" + c(str(archive_path), "bold"))
        return 1
    print(c("\n🔄 接力提示词已自动存入剪贴板！", "green") +
          f"（后端：{backend}，约 {estimate_tokens(next_prompt)} tokens）")
    if archive_path:
        print("   归档： " + c(str(archive_path), "dim"))
    print("   切回网页端按 " + c("Ctrl+V", "bold") + " 发送即可；拿到回复后回来按回车继续。")
    return -1


def cmd_catch(args) -> int:
    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(c(f"项目根目录不存在：{root}", "red"))
        return 2
    root = root.resolve()
    round_no = 0
    pending_input = args.input
    while True:
        round_no += 1
        print(c(f"\n=== 第 {round_no} 轮 · 接球 ===", "cyan"))
        if pending_input:
            try:
                text = Path(pending_input).expanduser().read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(c(f"读取回复文件失败：{exc}", "red"))
                return 2
            pending_input = None
            print(f"  从文件读取回复：{args.input}")
        else:
            if not args.no_wait:
                if not wait_enter("请把网页端的回复全选复制到剪贴板，然后按回车读取（输入 q 退出）："):
                    print("已退出。")
                    return 0
            try:
                text = clip_get()
            except Exception as exc:
                print(c(f"读取剪贴板失败：{exc}", "red"))
                return 1
        if not text.strip():
            print(c("剪贴板为空，请先复制网页端的回复。", "yellow"))
            if args.once:
                return 2
            if not sys.stdin.isatty():
                return 2
            continue

        # 必须在归档与解析之前：否则忘复制回复时会把发出的提示词当成回复存盘
        if _is_echo_of_last_outbound(root, text):
            print(c("剪贴板仍是上次发出的提示词，请先复制网页端回复。", "yellow"))
            if args.once or not sys.stdin.isatty():
                return 2
            continue

        archive_put(root, "in", text, round_no)
        reply = parse_reply(text)
        print(f"  解析结果：文件 {len(reply['files'])} 个，"
              f"命令 {len(reply['execs'])} 条，status={reply['status']}")
        has_contract = bool(reply["files"] or reply["execs"]
                            or reply["status_raw"] or reply["summary"])
        if not has_contract:
            preview = re.sub(r"\s+", " ", text.strip().lstrip("﻿"))[:200]
            print(c("未发现任何契约标签，无法落地。回复开头：", "red") + preview)
            print("  请确认网页端是否按契约输出；必要时重新 pack 一次。")
            if args.once:
                return 2
            if not sys.stdin.isatty():
                return 2
            continue

        result = _decide_and_next(root, reply, round_no, args)
        if result >= 0:
            return result
        if args.once:
            msg = ("（--once：已写入下一轮提示词，本次结束）" if result == -1
                   else "（--once：本轮已取消，结束）")
            print(c(msg, "yellow"))
            return 0
        if not sys.stdin.isatty():
            print(c("非交互环境不再继续等待，已结束。", "yellow"))
            return 0
        # result == -2（用户取消）时继续下一轮等待


def cmd_relay(args) -> int:
    code = cmd_pack(args)
    if code != 0:
        return code
    print(c("\n发球完成，进入接球循环。", "cyan"))
    return cmd_catch(args)


# ============================================================================
# 十、自测
# ============================================================================
SAMPLE_REPLY = """```xml
好的，下面是修改后的文件。

<file path="src/main.py">
# -*- coding: utf-8 -*-
def compute(x, y):
    if y == 0:
        return 0.0
    return x / y
</file>

<file path='utils.py'>
def add(a, b):
    return a + b
</file>

<exec>python -m pytest -q</exec>
<exec>python src/main.py</exec>

<status> done </status>
<summary>修复了除零异常，并补充了工具函数。</summary>
```
"""


def _compiles(source: str) -> bool:
    try:
        compile(source, "<selftest>", "exec")
        return True
    except SyntaxError:
        return False


def run_selftests() -> int:
    import shutil
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(("  [PASS] " if cond else c("  [FAIL] ", "red")) + name)

    print(c("== gemini_relay.py 自测 ==", "bold"))

    # 1) 解析：围栏噪声、单双引号、多文件、status/summary
    reply = parse_reply(SAMPLE_REPLY)
    check("解析出 2 个文件", len(reply["files"]) == 2)
    check("解析出 2 条命令", len(reply["execs"]) == 2)
    check("status 归一化为 done", reply["status"] == "done")
    check("summary 正常提取", reply["summary"].startswith("修复了除零异常"))
    check("文件内容含函数体", "def compute" in dict(reply["files"])["src/main.py"])
    check("文件内容无围栏残留", "```" not in dict(reply["files"])["src/main.py"])
    check("单引号路径可解析", dict(reply["files"])["utils.py"].endswith("\n"))

    # 2) 正文中出现 </file> 字面量时不误截断
    tricky = ('<file path="a.py">\nSENTINEL = "</file>\n"\nprint(SENTINEL)\n</file>\n'
              '<status>continue</status>')
    parsed = parse_reply(tricky)
    check("字面量 </file> 不误截断", 'print(SENTINEL)' in dict(parsed["files"])["a.py"])

    # 3) 路径安全
    tmp_root = Path(tempfile.mkdtemp(prefix="relay_selftest_"))
    try:
        check("拒绝 .. 越级", safe_target(tmp_root, "../evil.py")[0] is None)
        check("拒绝绝对路径", safe_target(tmp_root, "C:/Windows/x.py")[0] is None)
        check("拒绝 UNC/根路径", safe_target(tmp_root, "/etc/passwd")[0] is None)
        ok_target, _ = safe_target(tmp_root, "pkg/sub/a.py")
        check("接受合法相对路径", ok_target is not None and
              str(ok_target).startswith(str(tmp_root.resolve())))

        # 4) 危险命令
        for bad in ("rm -rf /", "del /f /s /q C:\\*", "format C:", "diskpart",
                    "git reset --hard HEAD~3", "rd /s /q C:\\Windows"):
            check(f"拦截危险命令：{bad[:18]}", danger_check(bad))
        for good in ("python -m pytest -q", "npm test", "python main.py --verbose"):
            check(f"放行安全命令：{good}", not danger_check(good))

        # 5) token 估算
        check("英文 token 估算合理", 90 <= estimate_tokens("a" * 400) <= 130)
        check("中文 token 估算合理", 90 <= estimate_tokens("中" * 100) <= 110)

        # 6) 目录树
        (tmp_root / "pkg").mkdir()
        (tmp_root / ".git").mkdir()
        (tmp_root / ".git" / "config").write_text("x", encoding="utf-8")
        (tmp_root / "pkg" / "mod.py").write_text("print(1)\n", encoding="utf-8")
        (tmp_root / "main.py").write_text("print(2)\n", encoding="utf-8")
        tree = build_tree(tmp_root)
        check("目录树含文件", "main.py" in tree and "pkg/" in tree)
        check("目录树忽略 .git", ".git" not in tree)

        # 7) 打包提示词
        entries, notes = collect_files(["main.py", "pkg"], tmp_root, 200000, 40)
        check("collect_files 收集 2 个文件", len(entries) == 2)
        prompt = build_pack_prompt(tmp_root, entries, "修复问题", tree, "补充")
        for tag in ("<file path=", "<exec>", "<status>", "<summary>"):
            check(f"提示词包含契约 {tag}", tag in prompt)
        check("提示词包含任务", "修复问题" in prompt)

        # 8) plan + commit（新建嵌套目录 / 修改统计 / 无变化判定）
        plan = plan_files([("pkg/new/deep.py", "x = 1\n"),
                           ("main.py", "print(3)\n"),
                           ("main.py", "print(2)\n")], tmp_root)
        check("新建文件标记正确", plan[0]["new_file"] and plan[0]["ok"])
        check("修改统计 +1 -1", plan[1]["added"] == 1 and plan[1]["removed"] == 1)
        check("无变化被识别", plan[2]["unchanged"])
        commit_plan(plan, tmp_root)
        check("嵌套父目录已创建", (tmp_root / "pkg" / "new" / "deep.py").exists())
        committed = (tmp_root / "main.py").read_text(encoding="utf-8")
        check("原子写入生效", committed == "print(3)\n")
        check("临时文件已清理",
              not list((tmp_root / "pkg" / "new").glob("*.relaytmp")))
        check("无变化文件未被改写", (tmp_root / "main.py").read_text(encoding="utf-8") == "print(3)\n")

        # 9) 续球提示词
        reports = [{"cmd": "python src/main.py", "rc": 1,
                    "output": "Traceback: ZeroDivisionError", "blocked": False}]
        nxt = build_next_prompt(tmp_root, 2, ["main.py"], [], reports, "修复", "continue")
        check("续球提示词含报错原因", "诊断并输出修复补丁" in nxt)
        check("续球提示词含报错输出", "ZeroDivisionError" in nxt)
        check("续球提示词含文件最新内容", "print(3)" in nxt)
        blocked = [{"cmd": "rm -rf /", "rc": -99, "output": "",
                    "blocked": True, "reason": "危险"}]
        nxt2 = build_next_prompt(tmp_root, 3, [], [], blocked, "x", "continue")
        check("续球提示词记录被拦截命令", "被本地安全门禁拦截" in nxt2)

        # 10) <file> 正文内的标签字面量不得被当成真实标签
        nested = ('<file path="tool.py">\n'
                  'CMD = "<exec>rm -rf /</exec>"\n'
                  'STATE = "<status>done</status>"\n'
                  '</file>\n'
                  '<exec>python real.py</exec>\n<status>continue</status>')
        np = parse_reply(nested)
        check("正文 <exec> 字面量不被执行", np["execs"] == ["python real.py"])
        check("正文 <status> 字面量不篡改状态", np["status"] == "continue")

        # 11) 敏感文件过滤
        for name in (".env", ".env.local", "server.pem", "id_rsa", "credentials.json"):
            (tmp_root / name).write_text("SECRET=1\n", encoding="utf-8")
        (tmp_root / ".env.example").write_text("KEY=\n", encoding="utf-8")
        sec, _ = collect_files(["."], tmp_root, 200000, 40)
        sec_names = {Path(e["rel"]).name for e in sec}
        check("凭证文件被跳过", not (sec_names & {".env", ".env.local",
                                              "server.pem", "id_rsa",
                                              "credentials.json"}))
        check(".env.example 被放行", ".env.example" in sec_names)
        forced, _ = collect_files(["."], tmp_root, 200000, 40, include_secrets=True)
        check("--include-secrets 放行凭证",
              ".env" in {Path(e["rel"]).name for e in forced})
        single, _ = collect_files([".env"], tmp_root, 200000, 40)
        check("显式单文件放行", any(e["rel"].endswith(".env") for e in single))

        NBSP, ZWSP, BOM = chr(0x00A0), chr(0x200B), chr(0xFEFF)
        # 12) 不可见字符清洗
        dirty = ('<file path="a.py">\n\nx' + NBSP + '=' + NBSP + '1' + ZWSP + '\n</file>\n<status>done</status>')
        dp = parse_reply(dirty)
        body = dict(dp["files"])["a.py"]
        check("NBSP 已替换为空格", NBSP not in body)
        check("零宽空格已删除", ZWSP not in body)
        check("开标签后 BOM 已剥离",
              not dict(parse_reply('<file path="b.py">\n' + BOM + 'x = 1\n</file>')
                       ["files"])["b.py"].startswith(BOM))
        check("清洗后仍可编译", _compiles(body))

        # 13) 归档轮次成对推导
        arc = tmp_root / ARCHIVE_DIR / "demo"
        arc.mkdir(parents=True)
        check("空目录从第 1 轮起", next_round(arc, "out") == 1)
        _atomic_write(arc / f"r001-out-{_utc_stamp()}.md", "x")
        check("out 之后 in 复用同轮", next_round(arc, "in") == 1)
        _atomic_write(arc / f"r001-in-{_utc_stamp()}.md", "y")
        check("in 已配对后 out 递增", next_round(arc, "out") == 2)
        _atomic_write(arc / f"r999-out-{_utc_stamp()}.md", "z")
        check("三位以上轮次仍可推导", next_round(arc, "out") == 1000)

        # 14) 自触发拦截
        _write_outbound_hash(tmp_root, "PROMPT-BODY")
        check("哈希命中识别为回显", _is_echo_of_last_outbound(tmp_root, "PROMPT-BODY"))
        check("换行差异仍命中", _is_echo_of_last_outbound(tmp_root, "PROMPT-BODY\r\n\r\n"))
        check("正常回复不误判", not _is_echo_of_last_outbound(tmp_root, "网页端回复"))

        # 15) 归档目录护栏
        check("safe_target 拒绝 .web-relay",
              safe_target(tmp_root, f"{ARCHIVE_DIR}/x.md")[0] is None)
        check("大小写变体也拒绝",
              safe_target(tmp_root, f"{ARCHIVE_DIR.upper()}/x.md")[0] is None)
        check("归一化路径也拒绝",
              safe_target(tmp_root, f"sub/../{ARCHIVE_DIR}/x.md")[0] is None)
        (arc / "leak.txt").write_text("ARCHIVE", encoding="utf-8")
        packed, _ = collect_files(["."], tmp_root, 200000, 40)
        check("归档不进打包结果",
              not any(ARCHIVE_DIR in e["rel"] for e in packed))
        check("归档不进目录树", ARCHIVE_DIR not in build_tree(tmp_root))

        # 16) 覆写前备份
        (tmp_root / "keep.py").write_text("OLD\n", encoding="utf-8")
        commit_plan(plan_files([("keep.py", "NEW\n")], tmp_root), tmp_root, 7)
        backups = list((tmp_root / ARCHIVE_DIR / "backups").glob("*-r007/keep.py"))
        check("覆写前生成备份", len(backups) == 1)
        check("备份内容是旧版", backups and backups[0].read_text(encoding="utf-8") == "OLD\n")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    failed = [name for name, ok in checks if not ok]
    print()
    if failed:
        print(c(f"自测未通过：{len(failed)}/{len(checks)} 项失败", "red"))
        for name in failed:
            print("  - " + name)
        return 1
    print(c(f"自测全部通过：{len(checks)}/{len(checks)} 项", "green"))
    return 0


# ============================================================================
# 十一、命令行入口
# ============================================================================
EPILOG = """\
示例：
  python gemini_relay.py pack -f main.py utils.py -p "修复计算精度问题"
  python gemini_relay.py pack -f src/ -p "补类型注解" --out prompt.txt --stdout
  python gemini_relay.py catch                 # 读剪贴板，落地+执行+自动续球
  python gemini_relay.py catch --once -y        # 只做一轮
  python gemini_relay.py relay -f . -p "修复所有 lint 问题"
  python gemini_relay.py selftest

工作流：
  1. pack 把 文件快照 + 任务 + 输出契约 打进剪贴板；
  2. 到网页端粘贴发送，把回复整体复制；
  3. catch 解析回复 -> 展示变更 -> 确认 -> 落盘 -> 执行 <exec>；
  4. 报错或 status=continue 时自动生成下一轮提示词并写进剪贴板，回到第 2 步。
"""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=".", help="项目根目录（默认当前目录）")

    pack_opts = argparse.ArgumentParser(add_help=False)
    pack_opts.add_argument("-f", "--files", nargs="+", help="要打包的文件或目录（可多个）")
    pack_opts.add_argument("-p", "--prompt", help="任务描述")
    pack_opts.add_argument("--extra", default="", help="追加给模型的补充说明")
    pack_opts.add_argument("--max-file-bytes", type=int, default=200_000,
                           help="单文件打包上限字节数（默认 200000）")
    pack_opts.add_argument("--max-files", type=int, default=40,
                           help="最多打包文件数（默认 40）")
    pack_opts.add_argument("--warn-tokens", type=int, default=150_000,
                           help="token 体量告警阈值（默认 150000）")
    pack_opts.add_argument("--out", help="同时把提示词写入指定文件")
    pack_opts.add_argument("--stdout", action="store_true", help="同时打印提示词全文")

    catch_opts = argparse.ArgumentParser(add_help=False)
    catch_opts.add_argument("-y", "--yes", action="store_true", help="自动确认落盘")
    catch_opts.add_argument("--once", action="store_true", help="只处理一轮")
    catch_opts.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                            help="单条命令超时秒数（默认 600）")
    catch_opts.add_argument("--allow-dangerous", action="store_true",
                            help="命中危险命令时改为逐条人工确认")
    catch_opts.add_argument("--input", help="从文件读取回复（默认读剪贴板），便于测试")
    catch_opts.add_argument("--no-exec", action="store_true", help="只落盘，不执行 <exec>")
    catch_opts.add_argument("--no-wait", action="store_true",
                            help="不等待回车，直接读剪贴板")

    parser = argparse.ArgumentParser(
        prog="gemini_relay.py",
        description="网页端大模型与本地智能体的人在回路问答接力脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_pack = sub.add_parser("pack", parents=[common, pack_opts],
                            help="打包文件与任务，组装提示词并写入剪贴板")
    p_pack.set_defaults(func=cmd_pack)

    p_catch = sub.add_parser("catch", parents=[common, catch_opts],
                             help="解析剪贴板回复，落盘并执行验证命令")
    p_catch.set_defaults(func=cmd_catch)

    p_relay = sub.add_parser("relay", parents=[common, pack_opts, catch_opts],
                             help="pack 后直接进入 catch 多轮循环")
    p_relay.set_defaults(func=cmd_relay)

    p_test = sub.add_parser("selftest", help="运行内置回归自测")
    p_test.set_defaults(func=lambda _args: run_selftests())

    return parser


def main(argv=None) -> int:
    _setup_console()
    _init_color()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print(c("\n已中断。", "yellow"))
        return 130
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1


if __name__ == "__main__":
    sys.exit(main())
