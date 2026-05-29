import multiprocessing
import os
import sys
import threading
import time

BUSYBOX_COMMANDS = [
    "ls",
    "pwd",
    "cat",
    "wc",
    "sort",
    "uniq",
    "echo",
    "df",
    "du",
    "base64",
    "basename",
    "dirname",
    "env",
    "sleep",
    "grep",
    "tr",
    "seq",
    "awk",
    "head",
    "tail",
    "sh",
    "sed",
    "strings",
    "hexdump",
    "timeout",
]
STANDALONE_COMMANDS = ["rg", "jq", "gron"]


def apply_path_jail(bin_dir: str, workspace: str):
    bin_dir = os.path.abspath(bin_dir)
    jailed_bin = os.path.abspath(os.path.join(workspace, ".jailed_bin"))
    os.makedirs(jailed_bin, exist_ok=True)

    if sys.platform == "win32":
        bb_src = os.path.join(bin_dir, "busybox.exe")
        if os.path.exists(bb_src):
            for cmd in BUSYBOX_COMMANDS:
                dst = os.path.join(jailed_bin, f"{cmd}.exe")
                if not os.path.exists(dst):
                    try:
                        os.link(bb_src, dst)
                    except OSError:
                        import shutil

                        shutil.copy2(bb_src, dst)

    for cmd in STANDALONE_COMMANDS:
        exe_name = f"{cmd}.exe" if sys.platform == "win32" else cmd
        src = os.path.join(bin_dir, exe_name)
        dst = os.path.join(jailed_bin, exe_name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.link(src, dst)
            except OSError:
                import shutil

                shutil.copy2(src, dst)

    if sys.platform == "win32":
        os.environ["PATH"] = jailed_bin
        os.environ["COMSPEC"] = os.path.join(jailed_bin, "sh.exe")
    else:
        os.environ["PATH"] = jailed_bin + os.pathsep + os.environ.get("PATH", "/usr/bin")
        os.environ["SHELL"] = "/bin/sh"


def _win_poller_worker(interrupt_event):
    import _thread

    while True:
        if interrupt_event.wait(1.0):
            interrupt_event.clear()
            _thread.interrupt_main()


def ipython_worker(
    command_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    working_dir: str,
    output_file: str,
    interrupt_event,
    bin_path: str,
):
    if sys.platform != "win32":
        import signal

        os.setpgrp()
        signal.signal(signal.SIGINT, signal.default_int_handler)

    try:
        os.chdir(os.path.realpath(working_dir))
    except Exception as e:
        result_queue.put({"status": "crash", "exit_code": 1, "ret_val": f"Failed to enter directory: {e}"})
        return

    if bin_path and os.path.exists(bin_path):
        apply_path_jail(bin_path, working_dir)

    if sys.platform == "win32" and interrupt_event is not None:
        poller = threading.Thread(target=_win_poller_worker, args=(interrupt_event,), daemon=True)
        poller.start()

    from IPython.core.interactiveshell import InteractiveShell
    from IPython.utils.capture import capture_output
    from IPython.utils.text import FullEvalFormatter
    from traitlets.config import Config

    class JinjaFormatter(FullEvalFormatter):
        """
        A formatter that only expands variables inside {{ }} (Jinja2 style).
        Single { } and $ are treated as literal text, making it safe for regex and awk.
        """

        def parse(self, fmt_string: str):
            pos = 0
            length = len(fmt_string)

            while pos < length:
                # 1. TEXT MODE: Find the next '{{'
                start_idx = fmt_string.find("{{", pos)

                if start_idx == -1:
                    # No more variables found, yield the remaining string as literal text
                    yield (fmt_string[pos:], None, None, None)
                    break

                # Yield the literal text up to the '{{'
                literal_text = fmt_string[pos:start_idx]

                # 2. VARIABLE MODE: We are now inside {{ ... }}
                # Move the position past the '{{'
                pos = start_idx + 2
                field_name_start = pos

                in_string = False
                string_char = None
                escaped = False

                # Scan forward to find the closing '}}', respecting string boundaries
                while pos < length:
                    char = fmt_string[pos]

                    if in_string:
                        # 3. STRING MODE: We are inside quotes
                        if escaped:
                            escaped = False
                        elif char == "\\":
                            escaped = True
                        elif char == string_char:
                            in_string = False
                    else:
                        # Not in a string, check for string start or variable end
                        if char in ('"', "'"):
                            in_string = True
                            string_char = char
                        elif char == "}" and pos + 1 < length and fmt_string[pos + 1] == "}":
                            # Found the valid, unquoted closing '}}'
                            break

                    pos += 1

                if pos >= length:
                    # Reached the end of the input without finding '}}'
                    raise ValueError("Missing closing '}}' for variable block")

                # Extract the field name (the expression to be evaluated by FullEvalFormatter)
                field_name = fmt_string[field_name_start:pos].strip()

                # Yield the parsed segment
                # (literal_text, field_name, format_spec, conversion)
                yield (literal_text, field_name, "", None)

                # Move position past the closing '}}' to resume TEXT MODE
                pos += 2

    InteractiveShell.var_expand.__defaults__ = (0, JinjaFormatter())

    c = Config()
    c.InteractiveShell.colors = "nocolor"
    c.InteractiveShell.color_info = False
    c.HistoryManager.enabled = False
    c.InteractiveShell.profile_dir = os.path.join(working_dir, ".ipython_profile")

    shell = InteractiveShell.instance(config=c)

    if hasattr(shell, "enable_matplotlib"):
        try:
            shell.enable_matplotlib("inline")
        except Exception:
            pass

    shell.displayhook.write_output_prompt = lambda: None
    shell.displayhook.write_format_data = lambda *args, **kwargs: None

    if sys.platform == "win32":
        import glob
        import shlex
        import subprocess

        from IPython.utils.text import SList

        sh_path = os.path.join(os.environ["PATH"], "sh.exe") if "PATH" in os.environ else "sh.exe"

        def chunk_windows_command(cmd):
            try:
                args = shlex.split(cmd, posix=False)
            except Exception:
                return [cmd]

            wildcard_indices = [i for i, arg in enumerate(args) if "*" in arg or "?" in arg]
            if not wildcard_indices:
                return [cmd]

            first_wc = wildcard_indices[0]
            last_wc = wildcard_indices[-1]

            prefix_args = [args[i] for i in range(first_wc)]
            suffix_args = [args[i] for i in range(last_wc + 1, len(args))]

            chunkable_files = []
            has_expanded = False
            for i in range(first_wc, last_wc + 1):
                arg = args[i]
                clean_arg = arg
                if (arg.startswith('"') and arg.endswith('"')) or (arg.startswith("'") and arg.endswith("'")):
                    clean_arg = arg[1:-1]
                files = glob.glob(clean_arg)
                if files:
                    has_expanded = True
                    for f in files:
                        f_norm = f.replace("\\", "/")
                        if " " in f_norm:
                            chunkable_files.append(f'"{f_norm}"')
                        else:
                            chunkable_files.append(f_norm)
                else:
                    chunkable_files.append(arg)

            if not has_expanded:
                return [cmd]

            prefix_str = " ".join(prefix_args)
            suffix_str = " ".join(suffix_args)
            base_len = len(prefix_str) + len(suffix_str) + 2

            chunks = []
            current_chunk = []
            current_len = base_len

            for f in chunkable_files:
                item_len = len(f) + 1
                if current_len + item_len > 30000:
                    if current_chunk:
                        chunks.append(current_chunk)
                        current_chunk = []
                        current_len = base_len
                current_chunk.append(f)
                current_len += item_len
            if current_chunk:
                chunks.append(current_chunk)

            cmd_strings = []
            for chunk in chunks:
                parts = []
                if prefix_args:
                    parts.append(prefix_str)
                parts.append(" ".join(chunk))
                if suffix_args:
                    parts.append(suffix_str)
                cmd_strings.append(" ".join(parts))

            return cmd_strings

        def busybox_system(cmd):
            cmd = shell.var_expand(cmd, depth=1)
            cmds = chunk_windows_command(cmd)
            exit_codes = []
            for cmd_str in cmds:
                p = subprocess.Popen(
                    [sh_path, "-c", cmd_str],
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    stdin=subprocess.DEVNULL,
                )
                try:
                    while p.poll() is None:
                        time.sleep(0.05)
                    exit_codes.append(p.returncode)
                except KeyboardInterrupt:
                    p.terminate()
                    try:
                        p.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        p.kill()
                    raise
            shell.user_ns["_exit_code"] = next((code for code in exit_codes if code != 0), 0)

        def busybox_getoutput(cmd, split=True, depth=0):
            cmd = shell.var_expand(cmd, depth=depth + 1)
            cmds = chunk_windows_command(cmd)
            all_outputs = []
            for cmd_str in cmds:
                out = ""
                try:
                    p = subprocess.Popen(
                        [sh_path, "-c", cmd_str],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        stdin=subprocess.DEVNULL,
                    )
                    out_chunks = []

                    def reader(proc=p, chunks=out_chunks):
                        try:
                            while True:
                                chunk = proc.stdout.read(1024)
                                if not chunk:
                                    break
                                chunks.append(chunk)
                        except Exception:
                            pass

                    t_read = threading.Thread(target=reader, daemon=True)
                    t_read.start()
                    while p.poll() is None:
                        time.sleep(0.05)
                    t_read.join(timeout=0.2)
                    out = "".join(out_chunks)
                except KeyboardInterrupt:
                    p.terminate()
                    try:
                        p.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        p.kill()
                    raise
                except Exception as e:
                    out = str(e)
                all_outputs.append(out)

            combined_out = "".join(all_outputs)
            if split:
                return SList(combined_out.splitlines())
            return combined_out

        if os.path.exists(sh_path):
            shell.system = busybox_system
            shell.getoutput = busybox_getoutput

    while True:
        try:
            cell_id, command = command_queue.get()
            if cell_id == "__PING__":
                result_queue.put({"status": "success", "exit_code": 0, "ret_val": "PONG"})
                continue

            with open(output_file, "a", encoding="utf-8", buffering=1) as f:
                original_stdout_fd = os.dup(1)
                original_stderr_fd = os.dup(2)
                original_stdout = sys.stdout
                original_stderr = sys.stderr

                os.dup2(f.fileno(), 1)
                os.dup2(f.fileno(), 2)
                sys.stdout = f
                sys.stderr = f

                captured_outputs = []
                try:
                    with capture_output(stdout=False, stderr=False, display=True) as captured:
                        result = shell.run_cell(command, cell_id=cell_id)
                    captured_outputs = captured.outputs
                except KeyboardInterrupt:
                    raise
                finally:
                    if hasattr(sys.stdout, "flush"):
                        sys.stdout.flush()
                    if hasattr(sys.stderr, "flush"):
                        sys.stderr.flush()
                    sys.stdout = original_stdout
                    sys.stderr = original_stderr
                    os.dup2(original_stdout_fd, 1)
                    os.dup2(original_stderr_fd, 2)
                    os.close(original_stdout_fd)
                    os.close(original_stderr_fd)

            ret_val = ""
            error = result.error_in_exec or result.error_before_exec
            if error:
                pass
            elif result.result is not None:
                ret_val = repr(result.result)

            rich_info = []
            for idx, out in enumerate(captured_outputs):
                if "image/png" in out.data:
                    import base64

                    img_data = base64.b64decode(out.data["image/png"])
                    img_name = f"{cell_id}_img_{idx}.png"
                    try:
                        with open(img_name, "wb") as img_f:
                            img_f.write(img_data)
                        rich_info.append(f"[Image generated: {img_name}]")
                    except Exception as e:
                        rich_info.append(f"[Image generation failed: {e}]")

            if rich_info:
                if ret_val:
                    ret_val += "\n"
                ret_val += "\n".join(rich_info)

            result_queue.put(
                {
                    "status": "error" if error else "success",
                    "exit_code": 1 if error else 0,
                    "ret_val": ret_val,
                }
            )

        except KeyboardInterrupt:
            result_queue.put({"status": "error", "exit_code": 1, "ret_val": "KeyboardInterrupt"})
        except BaseException as e:
            result_queue.put({"status": "crash", "exit_code": 1, "ret_val": f"Shell Error: {str(e)}"})
