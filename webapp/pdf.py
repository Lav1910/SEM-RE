"""Print the HBM-branded report (report.html) to a US Letter PDF with headless Chrome/Chromium."""
import os
import shutil
import subprocess
import tempfile

CANDIDATES = [os.environ.get("HBM_CHROME", ""), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              shutil.which("chromium") or "", shutil.which("chromium-browser") or "", shutil.which("google-chrome") or ""]


def chrome():
    for c in CANDIDATES:
        if c and os.path.exists(c):
            return c
    raise RuntimeError("Chrome or Chromium is not installed on the server")


def html_to_pdf(html_path, pdf_path, timeout=120):
    profile = tempfile.mkdtemp(prefix="hbm-pdf-")
    tmp = pdf_path + ".tmp"
    try:
        subprocess.run([chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                        f"--user-data-dir={profile}", "--no-pdf-header-footer", "--virtual-time-budget=15000",
                        "--run-all-compositor-stages-before-draw", f"--print-to-pdf={tmp}", "file://" + os.path.abspath(html_path)],
                       check=True, capture_output=True, timeout=timeout)
        if not os.path.exists(tmp) or os.path.getsize(tmp) < 1000:
            raise RuntimeError("Chrome did not produce a PDF")
        os.replace(tmp, pdf_path)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        if os.path.exists(tmp):
            os.remove(tmp)
    return pdf_path
