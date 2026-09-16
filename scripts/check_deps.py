#!/usr/bin/env python3
"""依赖完整性自检。

开发时临时 pip install 的库很容易忘了写进 requirements.txt——
本机跑得好好的，一进容器就 ModuleNotFoundError。放进 CI 早点暴露。
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# import 名与包名不一致的情况
ALIAS = {"PIL": "pillow", "lark_oapi": "lark-oapi", "cv2": "opencv",
         "rapidocr_onnxruntime": "rapidocr", "dateutil": "python-dateutil",
         "yaml": "pyyaml"}


def third_party_imports():
    stdlib = set(sys.stdlib_module_names)
    used = set()
    for f in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                used |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                used.add(n.module.split(".")[0])
    return {m for m in used if m not in stdlib and not m.startswith("app")}


def main():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    missing = [m for m in sorted(third_party_imports())
               if ALIAS.get(m, m).lower() not in req]
    for m in sorted(third_party_imports()):
        ok = ALIAS.get(m, m).lower() in req
        print(f"  {m:<26}{'ok' if ok else 'MISSING'}")
    if missing:
        print(f"\n以下依赖被代码 import 但未写进 requirements.txt：{'、'.join(missing)}")
        return 1
    print("\n依赖声明完整")
    return 0


if __name__ == "__main__":
    sys.exit(main())
