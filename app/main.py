# -*- coding: utf-8 -*-
"""
Conversor de Texturas .ast — app Android (Kivy), sem Termux.

Converte texturas DDS / JPG / PNG (e afins) para .ast (container GS2D do
Farming Simulator 20 Android), usando o mesmo pipeline do fs2ast.py:
flip vertical, ASTC 6x6, header GS2D, auto-classificacao por nome
(generico 50%, specular 25%, terreno, etc.). O astcenc vai embutido no
APK como libastcenc.so.
"""

import os
import threading
import traceback

from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.switch import Switch
from kivy.uix.popup import Popup

import fs2ast

# paleta
BG = (0.09, 0.10, 0.12, 1)
CARD = (0.14, 0.15, 0.18, 1)
ACCENT = (0.15, 0.62, 0.42, 1)
ACCENT2 = (0.20, 0.45, 0.75, 1)
TXT = (0.92, 0.93, 0.95, 1)
SUB = (0.62, 0.65, 0.70, 1)

Window.clearcolor = BG


# ----------------- ambiente Android -----------------
def is_android():
    return "ANDROID_ARGUMENT" in os.environ


def request_all_files_access():
    if not is_android():
        return
    try:
        from android.permissions import request_permissions, Permission
        request_permissions([Permission.READ_EXTERNAL_STORAGE,
                             Permission.WRITE_EXTERNAL_STORAGE])
    except Exception:
        pass
    try:
        from jnius import autoclass
        Build = autoclass("android.os.Build$VERSION")
        if Build.SDK_INT >= 30:
            Environment = autoclass("android.os.Environment")
            if not Environment.isExternalStorageManager():
                Intent = autoclass("android.content.Intent")
                Settings = autoclass("android.provider.Settings")
                Uri = autoclass("android.net.Uri")
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                act = PythonActivity.mActivity
                intent = Intent(
                    Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
                intent.setData(Uri.parse("package:" + act.getPackageName()))
                act.startActivity(intent)
    except Exception:
        pass


def astcenc_path():
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        nld = PythonActivity.mActivity.getApplicationInfo().nativeLibraryDir
        for name in ("libastcenc.so", "libastcenc-neon.so"):
            p = os.path.join(nld, name)
            if os.path.exists(p):
                return p
    except Exception:
        pass
    return os.environ.get("ASTCENC", "astcenc")


def default_root():
    for p in ("/storage/emulated/0", os.path.expanduser("~"), "/"):
        if os.path.isdir(p):
            return p
    return "/"


# ----------------- navegador de pastas -----------------
class FolderBrowser(BoxLayout):
    def __init__(self, on_change, **kw):
        super().__init__(orientation="vertical", spacing=dp(4), **kw)
        self.on_change = on_change
        self.path = default_root()

        self.path_lbl = Label(text="", color=TXT, size_hint_y=None,
                              height=dp(30), halign="left", valign="middle",
                              font_size="13sp", shorten=True, markup=True)
        self.path_lbl.bind(size=lambda w, *_: setattr(
            w, "text_size", (w.width, None)))
        self.add_widget(self.path_lbl)

        sv = ScrollView()
        self.listbox = BoxLayout(orientation="vertical", size_hint_y=None,
                                 spacing=dp(2))
        self.listbox.bind(minimum_height=self.listbox.setter("height"))
        sv.add_widget(self.listbox)
        self.add_widget(sv)
        self.refresh()

    def refresh(self):
        self.listbox.clear_widgets()
        self.path_lbl.text = "[b]Pasta:[/b] " + self.path
        self.on_change(self.path)
        if os.path.dirname(self.path) != self.path:
            self.listbox.add_widget(self._row("..  (voltar)", self._up, ACCENT2))
        try:
            entries = sorted(os.listdir(self.path), key=str.lower)
        except Exception as e:
            self.listbox.add_widget(self._row("(sem acesso: %s)" % e,
                                              None, (0.5, 0.2, 0.2, 1)))
            return
        n_tex = sum(1 for f in entries
                    if os.path.splitext(f.lower())[1] in
                    (".dds", ".png", ".jpg", ".jpeg", ".bmp", ".tga"))
        if n_tex:
            self.listbox.add_widget(self._row(
                "%d textura(s) nesta pasta" % n_tex, None, ACCENT))
        for e in entries:
            full = os.path.join(self.path, e)
            if os.path.isdir(full):
                self.listbox.add_widget(self._row(
                    "> " + e, lambda p=full: self._enter(p), CARD))

    def _row(self, text, cb, color):
        b = Button(text=text, size_hint_y=None, height=dp(40),
                   background_normal="", background_color=color, color=TXT,
                   halign="left", valign="middle", font_size="13sp",
                   disabled=cb is None)
        b.bind(size=lambda w, *_: setattr(w, "text_size",
                                          (w.width - dp(16), None)))
        if cb:
            b.bind(on_release=lambda *_: cb())
        return b

    def _enter(self, p):
        self.path = p
        self.refresh()

    def _up(self):
        self.path = os.path.dirname(self.path)
        self.refresh()


class ConversorApp(App):
    def build(self):
        self.title = "Conversor de Texturas .ast"
        self.selected = default_root()
        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        root.add_widget(Label(text="[b]Conversor de Texturas .ast[/b]",
                              markup=True, color=TXT, font_size="20sp",
                              size_hint_y=None, height=dp(34)))
        root.add_widget(Label(text="DDS / JPG / PNG  ->  .ast  (FS20)",
                              color=SUB, font_size="12sp",
                              size_hint_y=None, height=dp(18)))

        self.browser = FolderBrowser(self._set_path, size_hint_y=0.44)
        root.add_widget(self.browser)

        opt = BoxLayout(orientation="vertical", size_hint_y=None,
                        height=dp(250), spacing=dp(4))
        self.sw_dds = self._switch_row(opt, "Converter DDS", True)
        self.sw_png = self._switch_row(opt, "Converter PNG", True)
        self.sw_jpg = self._switch_row(opt, "Converter JPG", True)
        self.sw_rec = self._switch_row(opt, "Incluir subpastas", True)
        self.sw_flip = self._switch_row(opt, "Flip vertical (FS20)", True)
        self.sw_del = self._switch_row(opt, "Apagar original apos converter", False)
        srow = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(38))
        srow.add_widget(Label(text="Escala genericos %:", color=TXT,
                              font_size="14sp", halign="left", valign="middle"))
        self.scale_in = TextInput(text="50", multiline=False, input_filter="int",
                                  size_hint_x=None, width=dp(90),
                                  background_color=CARD, foreground_color=TXT,
                                  cursor_color=TXT, font_size="14sp")
        srow.add_widget(self.scale_in)
        opt.add_widget(srow)
        root.add_widget(opt)

        self.run_btn = Button(text="CONVERTER PARA .AST", size_hint_y=None,
                              height=dp(54), background_normal="",
                              background_color=ACCENT, color=(1, 1, 1, 1),
                              font_size="16sp", bold=True)
        self.run_btn.bind(on_release=self._start)
        root.add_widget(self.run_btn)

        sv = ScrollView(size_hint_y=0.32)
        self.log_lbl = Label(text="", color=TXT, font_size="12sp",
                             size_hint_y=None, halign="left", valign="top")
        self.log_lbl.bind(size=lambda w, *_: setattr(
            w, "text_size", (w.width, None)))
        self.log_lbl.bind(texture_size=lambda w, *_: setattr(
            w, "height", w.texture_size[1]))
        self._sv = sv
        sv.add_widget(self.log_lbl)
        root.add_widget(sv)

        Clock.schedule_once(lambda *_: request_all_files_access(), 1)
        return root

    def _switch_row(self, parent, text, active):
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(34))
        lbl = Label(text=text, color=TXT, font_size="14sp", halign="left",
                    valign="middle")
        lbl.bind(size=lambda w, *_: setattr(w, "text_size", (w.width, None)))
        sw = Switch(active=active, size_hint_x=None, width=dp(80))
        row.add_widget(lbl)
        row.add_widget(sw)
        parent.add_widget(row)
        return sw

    def _set_path(self, p):
        self.selected = p

    @mainthread
    def log(self, msg):
        self.log_lbl.text += msg + "\n"
        Clock.schedule_once(lambda *_: setattr(self._sv, "scroll_y", 0), 0)

    @mainthread
    def _done(self, ok, extra=""):
        self._running = False
        self.run_btn.disabled = False
        self.run_btn.text = "CONVERTER PARA .AST"
        self.run_btn.background_color = ACCENT if ok else (0.6, 0.25, 0.25, 1)
        if extra:
            Popup(title="Concluido" if ok else "Erro",
                  content=Label(text=extra, color=TXT),
                  size_hint=(0.85, 0.4)).open()

    def _start(self, *_):
        if getattr(self, "_running", False):
            return
        self._running = True
        self.run_btn.disabled = True
        self.run_btn.text = "Convertendo..."
        self.log_lbl.text = ""
        exts = []
        if self.sw_dds.active:
            exts.append(".dds")
        if self.sw_png.active:
            exts.append(".png")
        if self.sw_jpg.active:
            exts += [".jpg", ".jpeg"]
        try:
            scale = max(1, min(100, int(self.scale_in.text or "50")))
        except ValueError:
            scale = 50
        threading.Thread(target=self._work,
                         args=(self.selected, tuple(exts), self.sw_rec.active,
                               self.sw_flip.active, self.sw_del.active, scale),
                         daemon=True).start()

    def _work(self, root, exts, recursive, flip, delete_src, scale):
        try:
            if not exts:
                self.log("Selecione ao menos um formato (DDS/PNG/JPG).")
                self._done(False, "Nenhum formato marcado.")
                return
            fs2ast.ASTCENC = astcenc_path()
            fs2ast.FLIPV = flip
            fs2ast.SCALE = scale / 100.0
            self.log("Pasta: %s" % root)
            self.log("astcenc: %s" % fs2ast.ASTCENC)
            self.log("Formatos: %s | subpastas: %s | escala: %d%%\n" %
                     (", ".join(exts), "sim" if recursive else "nao", scale))
            done, fail = fs2ast.convert_folder(
                root, log=self.log, only_exts=exts, recursive=recursive,
                delete_src=delete_src)
            self.log("\nPRONTO: %d convertidas, %d falhas." % (done, fail))
            self._done(fail == 0 or done > 0,
                       "%d textura(s) viraram .ast." % done)
        except Exception:
            self.log("\nERRO:\n" + traceback.format_exc())
            self._done(False, "Falhou. Veja o log.")


if __name__ == "__main__":
    ConversorApp().run()
