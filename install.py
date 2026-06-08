#!/usr/bin/env python3
"""
DOSE Home Station — One-Click Installer
========================================
This is the ONLY file you need to run.

HOW TO USE:
  1. Right-click this file
  2. Choose "Open with Thonny" (or just double-click if Thonny opens)
  3. Press the green ▶ Run button at the top of Thonny
  4. A window will appear — click the big INSTALL button
  5. Wait for it to finish
  6. Done! You'll have a DOSE icon on your desktop.
"""

import os
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import font as tkfont

REPO_URL = "https://github.com/relude117-star/doseconceptprototype.git"
INSTALL_DIR = os.path.expanduser("~/dose-home-station")
BRANCH = "claude/quirky-brown-vkHwi"


class Installer:
    def __init__(self, root):
        self.root = root
        self.root.title("DOSE Installer")
        self.root.geometry("500x400")
        self.root.configure(bg="#1a1a2e")
        self.root.resizable(False, False)

        try:
            fam = "DejaVu Sans"
        except Exception:
            fam = "Arial"

        tk.Label(
            root, text="DOSE", fg="#5B9BFF", bg="#1a1a2e",
            font=(fam, 48, "bold"),
        ).pack(pady=(40, 5))

        tk.Label(
            root, text="Home Station Installer", fg="#aaaaaa", bg="#1a1a2e",
            font=(fam, 14),
        ).pack(pady=(0, 30))

        self.btn = tk.Button(
            root, text="INSTALL", fg="white", bg="#5B9BFF",
            activebackground="#4a8aee", activeforeground="white",
            font=(fam, 22, "bold"), relief="flat",
            width=16, height=2, cursor="hand2",
            command=self.start_install,
        )
        self.btn.pack(pady=(0, 20))

        self.status = tk.Label(
            root, text="Click INSTALL to begin", fg="#888888", bg="#1a1a2e",
            font=(fam, 11), wraplength=460, justify="center",
        )
        self.status.pack(pady=(0, 10))

        self.progress = tk.Label(
            root, text="", fg="#5BD08A", bg="#1a1a2e",
            font=(fam, 10),
        )
        self.progress.pack()

    def set_status(self, text, color="#888888"):
        self.status.configure(text=text, fg=color)
        self.root.update_idletasks()

    def set_progress(self, text):
        self.progress.configure(text=text)
        self.root.update_idletasks()

    def run_cmd(self, cmd, description=""):
        if description:
            self.set_progress(description)
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Command failed: {cmd}\n{result.stderr}")
        return result.stdout

    def do_install(self):
        try:
            # Step 1: System packages
            self.set_status("Installing system packages...\nThis may take a few minutes.", "#5B9BFF")
            self.set_progress("[1/5] Updating package list...")
            self.run_cmd("sudo apt update -y")

            self.set_progress("[1/5] Installing packages...")
            self.run_cmd(
                "sudo apt install -y python3-tk python3-pil python3-pil.imagetk "
                "libzbar0 python3-picamera2 git"
            )

            # Step 2: Python packages
            self.set_status("Installing Python libraries...", "#5B9BFF")
            self.set_progress("[2/5] pip install...")
            self.run_cmd(
                "pip install --break-system-packages pyzbar Pillow 'qrcode[pil]' 2>/dev/null || "
                "pip install pyzbar Pillow 'qrcode[pil]'"
            )

            # Step 3: Clone or update repo
            self.set_status("Downloading DOSE app...", "#5B9BFF")
            self.set_progress("[3/5] Downloading from GitHub...")
            if os.path.isdir(os.path.join(INSTALL_DIR, ".git")):
                self.run_cmd(f"cd {INSTALL_DIR} && git pull origin {BRANCH} --ff-only 2>/dev/null || true")
            else:
                # Remove old install if exists but isn't a git repo
                if os.path.isdir(INSTALL_DIR):
                    self.run_cmd(f"rm -rf {INSTALL_DIR}")
                self.run_cmd(f"git clone -b {BRANCH} {REPO_URL} {INSTALL_DIR}")

            # If we're running from the extracted ZIP, copy files over
            script_dir = os.path.dirname(os.path.abspath(__file__))
            if script_dir != INSTALL_DIR and os.path.isfile(os.path.join(script_dir, "dose_demo.py")):
                self.run_cmd(f'cp -r "{script_dir}"/* "{INSTALL_DIR}/"')
                if os.path.isfile(os.path.join(script_dir, ".gitignore")):
                    self.run_cmd(f'cp "{script_dir}/.gitignore" "{INSTALL_DIR}/"')

            # Step 4: Make scripts executable
            self.set_status("Setting up shortcuts...", "#5B9BFF")
            self.set_progress("[4/5] Creating desktop shortcut...")
            self.run_cmd(f"chmod +x {INSTALL_DIR}/*.py {INSTALL_DIR}/*.sh 2>/dev/null || true")

            # Create desktop shortcut
            desktop_dir = os.path.expanduser("~/Desktop")
            os.makedirs(desktop_dir, exist_ok=True)
            desktop_path = os.path.join(desktop_dir, "DOSE.desktop")
            with open(desktop_path, "w") as f:
                f.write(f"""[Desktop Entry]
Type=Application
Name=DOSE Home Station
Comment=Launch DOSE medication dispenser
Exec=bash {INSTALL_DIR}/launch.sh
Icon={INSTALL_DIR}/dose_icon.png
Terminal=false
Categories=Utility;
StartupNotify=false
""")
            os.chmod(desktop_path, 0o755)

            # Trust the desktop file so it shows as an app, not a text file
            self.run_cmd(f'gio set "{desktop_path}" metadata::trusted true 2>/dev/null || true')

            # Step 5: Autostart on boot
            self.set_progress("[5/5] Setting up auto-start...")
            autostart_dir = os.path.expanduser("~/.config/autostart")
            os.makedirs(autostart_dir, exist_ok=True)
            autostart_path = os.path.join(autostart_dir, "dose-home-station.desktop")
            with open(autostart_path, "w") as f:
                f.write(f"""[Desktop Entry]
Type=Application
Name=DOSE Home Station
Exec=bash {INSTALL_DIR}/launch.sh
Terminal=false
X-GNOME-Autostart-enabled=true
""")

            # Create an app icon
            self._create_icon()

            # Done!
            self.set_status(
                "Installation complete!\n\n"
                "You now have a DOSE icon on your desktop.\n"
                "Double-click it to launch the app.\n"
                "It also starts automatically when you turn on your Pi.",
                "#5BD08A",
            )
            self.set_progress("")
            self.btn.configure(text="DONE ✓", bg="#5BD08A", state="disabled")

        except Exception as e:
            self.set_status(f"Error: {e}", "#FF6B6B")
            self.set_progress("Installation failed. Check your internet connection and try again.")
            self.btn.configure(text="RETRY", bg="#FF6B6B", state="normal")

    def _create_icon(self):
        """Create a simple DOSE app icon."""
        try:
            from PIL import Image, ImageDraw, ImageFont
            size = 128
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            # Blue rounded rectangle
            draw.rounded_rectangle(
                [4, 4, size - 4, size - 4],
                radius=24, fill="#5B9BFF",
            )
            # White "D" letter
            try:
                font = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72,
                )
            except OSError:
                font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), "D", font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(
                ((size - tw) // 2, (size - th) // 2 - 8),
                "D", fill="white", font=font,
            )
            icon_path = os.path.join(INSTALL_DIR, "dose_icon.png")
            img.save(icon_path)
        except Exception:
            pass

    def start_install(self):
        self.btn.configure(state="disabled", text="INSTALLING...", bg="#444466")
        thread = threading.Thread(target=self.do_install, daemon=True)
        thread.start()


def main():
    root = tk.Tk()
    Installer(root)
    root.mainloop()


if __name__ == "__main__":
    main()
