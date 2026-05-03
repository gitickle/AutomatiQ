import multiprocessing
import os
import sys
import threading
import time

BUSYBOX_COMMANDS = [
    "ls",
    "rm",
    "cp",
    "mv",
    "mkdir",
    "rmdir",
    "pwd",
    "cat",
    "wc",
    "sort",
    "uniq",
    "echo",
    "touch",
    "df",
    "du",
    "base64",
    "basename",
    "dirname",
    "env",
    "sleep",
    "grep",
    "tr",
    "tee",
    "mktemp",
    "seq",
    "awk",
    "head",
    "tail",
    "sh",
]
STANDALONE_COMMANDS = ["rg", "jq", "sd"]


def apply_path_jail(bin_dir: str, workspace: str):
    jailed_bin = os.path.join(workspace, ".jailed_bin")
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
    from traitlets.config import Config

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
        import subprocess

        from IPython.utils.text import SList

        sh_path = os.path.join(os.environ["PATH"], "sh.exe") if "PATH" in os.environ else "sh.exe"

        def busybox_system(cmd):
            p = subprocess.Popen([sh_path, "-c", cmd], stdout=sys.stdout, stderr=sys.stderr, stdin=subprocess.DEVNULL)
            try:
                while p.poll() is None:
                    time.sleep(0.05)
            except KeyboardInterrupt:
                p.terminate()
                try:
                    p.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    p.kill()
                raise

        def busybox_getoutput(cmd, split=True, depth=0):
            out = ""
            try:
                p = subprocess.Popen(
                    [sh_path, "-c", cmd],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdin=subprocess.DEVNULL,
                )
                out_chunks = []

                def reader():
                    try:
                        while True:
                            chunk = p.stdout.read(1024)
                            if not chunk:
                                break
                            out_chunks.append(chunk)
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
            if split:
                return SList(out.splitlines())
            return out

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
