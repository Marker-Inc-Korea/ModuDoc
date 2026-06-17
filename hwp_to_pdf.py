import sys
import os

def auto_click_popup():
    import threading, time
    def clicker():
        for _ in range(120):
            try:
                import win32gui, win32con, win32api

                hwp_dlg = [None]

                def find_hwp_window(hwnd, _):
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    title = win32gui.GetWindowText(hwnd)
                    if any(kw in title for kw in ['한글', '훈글', 'HWP']):
                        hwp_dlg[0] = hwnd
                        return False
                    return True

                win32gui.EnumWindows(find_hwp_window, None)

                if hwp_dlg[0] is None:
                    time.sleep(0.5)
                    continue

                dlg_hwnd = hwp_dlg[0]

                allow_buttons = []
                def find_allow_button(hwnd, _):
                    if win32gui.GetClassName(hwnd) == 'Button':
                        text = win32gui.GetWindowText(hwnd)
                        if '허용' in text and '안' not in text:
                            allow_buttons.append((hwnd, text))
                    return True

                try:
                    win32gui.EnumChildWindows(dlg_hwnd, find_allow_button, None)
                except Exception:
                    pass

                if allow_buttons:
                    target_hwnd, _ = next(
                        (b for b in allow_buttons if '모두' in b[1]),
                        allow_buttons[0]
                    )
                    win32gui.SetForegroundWindow(dlg_hwnd)
                    time.sleep(0.05)
                    win32gui.SendMessage(target_hwnd, win32con.BM_CLICK, 0, 0)
                    return
                else:
                    win32gui.SetForegroundWindow(dlg_hwnd)
                    time.sleep(0.1)
                    win32api.keybd_event(ord('N'), 0, 0, 0)
                    win32api.keybd_event(ord('N'), 0, win32con.KEYEVENTF_KEYUP, 0)
                    return

            except Exception:
                pass
            time.sleep(0.5)
    t = threading.Thread(target=clicker, daemon=True)
    t.start()


def main():
    if len(sys.argv) != 3:
        print("Usage: hwp_to_pdf.py <input_path> <output_pdf_path>", file=sys.stderr)
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    try:
        import pythoncom
        import win32com.client as win32
        pythoncom.CoInitialize()
    except ImportError:
        print("pywin32 미설치", file=sys.stderr)
        sys.exit(1)

    try:
        from win32com.client import gencache
        import glob, shutil
        gen_path = gencache.GetGeneratePath()
        if gen_path and os.path.exists(gen_path):
            hwp_clsid = "7D2B6F3C"
            for f in glob.glob(os.path.join(gen_path, f"*{hwp_clsid}*")):
                try:
                    if os.path.isdir(f):
                        shutil.rmtree(f, ignore_errors=True)
                    else:
                        os.remove(f)
                except Exception:
                    pass
            pycache = os.path.join(gen_path, "__pycache__")
            if os.path.exists(pycache):
                for f in glob.glob(os.path.join(pycache, f"*{hwp_clsid}*")):
                    try: os.remove(f)
                    except Exception: pass
    except Exception as e:
        print(f"gen_py 캐시 정리 중 오류 (무시): {e}", file=sys.stderr)

    abs_input = os.path.abspath(input_path).replace('/', '\\')
    abs_output = os.path.abspath(output_path).replace('/', '\\')

    hwp = None
    try:
        hwp = win32.DispatchEx("HWPFrame.HwpObject")
        try:
            hwp.SetMessageBoxMode(65535)
        except Exception:
            pass
        try:
            ret = hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule")
            print(f"RegisterModule result: {ret}", file=sys.stderr)
        except Exception as e:
            print(f"RegisterModule 실패: {e}", file=sys.stderr)

        auto_click_popup()
        hwp.Open(abs_input, "", "forceopen:true")

        hwp.SaveAs(abs_output, "PDF", "")

        hwp.Clear(1)
        hwp.Quit()

        if os.path.exists(abs_output):
            sys.exit(0)
        else:
            print("PDF 파일이 생성되지 않았습니다.", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"HWP COM 에러: {e}", file=sys.stderr)
        if hwp:
            try: hwp.Quit()
            except: pass
        sys.exit(1)
    finally:
        pythoncom.CoUninitialize()

if __name__ == "__main__":
    main()
