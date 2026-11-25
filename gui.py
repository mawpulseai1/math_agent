import json
import sys
import traceback
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from agent import MathAgent
from config import Config
from runtime import ensure_runtime


class StartupWorker(QThread):
    log = Signal(str)
    ready = Signal(dict)

    def run(self):
        try:
            st = ensure_runtime(Config(), emit=lambda s, t: self.log.emit(f"[{s}] {t}\n"))
            self.ready.emit(st)
        except Exception as e:
            self.log.emit(f"[startup] warning: {e}\n")
            self.ready.emit({})


class RunWorker(QThread):
    log = Signal(str)
    done = Signal(object, str)
    failed = Signal(str)

    def __init__(self, problem: str):
        super().__init__()
        self.problem = problem

    def run(self):
        try:
            agent = MathAgent(emit=lambda s, t: self.log.emit(f"[{s}] {t}\n"))
            rep = agent.run(self.problem)
            path, pdf = agent.export(rep)
            where = f"{path}" + (
                "  (PDF built)" if pdf else "  (LaTeX saved; install latexmk to build PDF)"
            )
            self.done.emit(rep, where)
        except Exception:
            self.failed.emit(traceback.format_exc())


class IngestWorker(QThread):
    log = Signal(str)

    def __init__(self, files: list[str]):
        super().__init__()
        self.files = files

    def run(self):
        agent = MathAgent(emit=lambda s, t: self.log.emit(f"[{s}] {t}\n"))
        for f in self.files:
            try:
                n = agent.vs.ingest(f)
                self.log.emit(f"ingested {f}: {n} chunks\n")
            except Exception as e:
                self.log.emit(f"failed {f}: {e}\n")


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Math Research Agent")
        self.resize(1200, 850)
        w = QWidget()
        self.setCentralWidget(w)
        lay = QVBoxLayout(w)

        lay.addWidget(QLabel("Enter a math problem:"))
        self.inp = QPlainTextEdit()
        self.inp.setMaximumHeight(110)
        lay.addWidget(self.inp)

        row = QHBoxLayout()
        self.btn = QPushButton("Research & Solve")
        self.btn.clicked.connect(self.start)
        self.add = QPushButton("Add papers to library…")
        self.add.clicked.connect(self.add_papers)
        row.addWidget(self.btn)
        row.addWidget(self.add)
        lay.addLayout(row)

        self.status = QLabel("")
        lay.addWidget(self.status)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.report = QTextBrowser()
        self.lean = QPlainTextEdit()
        self.lean.setReadOnly(True)
        self.numerics = QPlainTextEdit()
        self.numerics.setReadOnly(True)
        for widget, name in (
            (self.log, "Live log"),
            (self.report, "Report"),
            (self.lean, "Lean proof"),
            (self.numerics, "Numerics"),
        ):
            self.tabs.addTab(widget, name)

        self.status.setText("Checking runtime (Docker)...")
        self._startup = StartupWorker()
        self._startup.log.connect(self._append)
        self._startup.ready.connect(self._on_ready)
        self._startup.start()

    def _on_ready(self, st: dict):
        if self.btn.isEnabled():
            if st.get("docker_ok") and st.get("lean_image"):
                self.status.setText("Ready (Docker + Lean 4)")
            elif st.get("docker_ok"):
                self.status.setText("Ready (Docker Python OK; Lean image not built yet)")
            else:
                self.status.setText("Ready (Docker unavailable; check Live log)")

    def start(self):
        p = self.inp.toPlainText().strip()
        if not p:
            return
        self.btn.setEnabled(False)
        self.log.clear()
        self.status.setText("Running…")
        self.worker = RunWorker(p)
        self.worker.log.connect(self._append)
        self.worker.done.connect(self._finish)
        self.worker.failed.connect(self._fail)
        self.worker.start()

    def _append(self, text: str):
        self.log.insertPlainText(text)
        self.log.ensureCursorVisible()

    def _finish(self, rep, where: str):
        self.btn.setEnabled(True)
        self.status.setText(f"Saved to {where}")
        self.report.setMarkdown(rep.markdown)
        self.lean.setPlainText(rep.lean_code or rep.statement)
        self.numerics.setPlainText(
            (rep.experiment_code or "")
            + "\n\n"
            + json.dumps(rep.experiments, indent=2, ensure_ascii=False)
        )
        self.tabs.setCurrentWidget(self.report)

    def _fail(self, tb: str):
        self.btn.setEnabled(True)
        self.status.setText("Error (see live log)")
        self._append(tb)

    def add_papers(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add papers", "", "Papers (*.pdf *.txt *.md *.tex)"
        )
        if not files:
            return
        self.add.setEnabled(False)
        self.ing = IngestWorker(files)
        self.ing.log.connect(self._append)
        self.ing.finished.connect(lambda: self.add.setEnabled(True))
        self.ing.start()


def run_gui():
    app = QApplication(sys.argv)
    w = Window()
    w.show()
    sys.exit(app.exec())
