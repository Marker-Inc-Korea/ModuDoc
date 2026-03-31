"""
HWP/HWPX → PDF 변환 전용 헬퍼 스크립트.
Flask 백그라운드 스레드에서 HWP COM이 동작하지 않는 문제를 우회하기 위해
subprocess로 독립 실행됩니다.

사용법: python hwp_to_pdf.py <input_path> <output_pdf_path>
성공 시 exit code 0, 실패 시 exit code 1
"""
import sys
import os

def auto_click_popup():
    """HWP '접근 허용' 팝업 자동 처리.
    버튼 직접 클릭 시도 후 실패하면 키보드 단축키(N=모두 허용) 폴백."""
    import threading, time
    def clicker():
        for _ in range(120):  # 최대 60초
            try:
                import win32gui, win32con, win32api

                hwp_dlg = [None]

                def find_hwp_window(hwnd, _):
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    title = win32gui.GetWindowText(hwnd)
                    # 창 제목에 한글/훈글/HWP 포함 (스크린샷 기준 "한글")
                    if any(kw in title for kw in ['한글', '훈글', 'HWP']):
                        hwp_dlg[0] = hwnd
                        return False  # 첫 번째 매칭으로 중단
                    return True

                win32gui.EnumWindows(find_hwp_window, None)

                if hwp_dlg[0] is None:
                    time.sleep(0.5)
                    continue

                dlg_hwnd = hwp_dlg[0]

                # 자식 버튼 수집 (허용 안 함/모두 안 함 제외)
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
                    # "모두 허용" 우선 (모두 허용(N)), 없으면 "접근 허용(Y)"
                    target_hwnd, _ = next(
                        (b for b in allow_buttons if '모두' in b[1]),
                        allow_buttons[0]
                    )
                    win32gui.SetForegroundWindow(dlg_hwnd)
                    time.sleep(0.05)
                    win32gui.SendMessage(target_hwnd, win32con.BM_CLICK, 0, 0)
                    return
                else:
                    # 버튼 미탐지 → 키보드 단축키: N = 모두 허용(N)
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

    # gen_py 캐시 오염 시 자동 정리 (CLSIDToClassMap AttributeError 방지)
    # HWP COM CLSID에 해당하는 캐시 파일만 삭제
    try:
        from win32com.client import gencache
        import glob, shutil
        gen_path = gencache.GetGeneratePath()
        if gen_path and os.path.exists(gen_path):
            hwp_clsid = "7D2B6F3C"  # HWP COM CLSID 앞부분
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
        # HWP COM 자체의 메시지박스 자동 승인 — 보안/접근허용 팝업 포함 모든 다이얼로그 억제
        try:
            hwp.SetMessageBoxMode(65535)
        except Exception:
            pass
        try:
            ret = hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule")
            print(f"RegisterModule result: {ret}", file=sys.stderr)
        except Exception as e:
            print(f"RegisterModule 실패: {e}", file=sys.stderr)

        auto_click_popup()  # 파일 열기 전에 팝업 감시 스레드 시작
        # 파일 타입을 빈 문자열로 두면 HWP가 확장자/내용으로 자동 감지 (HWP/HWPX 공통)
        hwp.Open(abs_input, "", "forceopen:true")

        # SaveAs 방식: 1페이지씩 정상 출력 (FileSaveAsPdf 매크로는 2-up 모아찍기 적용될 수 있음)
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
