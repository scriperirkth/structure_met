from pathlib import Path
import argparse
import hashlib

parser = argparse.ArgumentParser()
parser.add_argument("source", help="Исходный XML-файл")
parser.add_argument("--mb", type=float, default=50, help="Размер части в МБ")
parser.add_argument("--out", default="readable", help="Выходная папка")
args = parser.parse_args()

source = Path(args.source).resolve()
out = Path(args.out).resolve()
limit = int(args.mb * 1_000_000)

if not source.is_file():
    raise SystemExit(f"Файл не найден: {source}")
if limit < 4:
    raise SystemExit("Слишком маленький размер части.")
if out.exists():
    raise SystemExit(
        f"Папка уже существует: {out}\n"
        "Укажи другую через --out, чтобы не перезаписать файлы."
    )

# Проверяем UTF-8 до создания выходных файлов.
# Исходный XML не изменяется.
try:
    with source.open("r", encoding="utf-8", newline="") as f:
        while f.read(1024 * 1024):
            pass
except UnicodeError:
    raise SystemExit(
        "Файл не в UTF-8. Этот скрипт рассчитан на UTF-8; "
        "не меняй кодировку вслепую."
    )

out.mkdir(parents=True)

rows = []
digest = hashlib.sha256()
offset = 0
number = 0

with source.open("rb") as f:
    while True:
        data = f.read(limit)
        if not data:
            break

        # Не разрезаем многобайтный символ UTF-8 между файлами.
        if len(data) == limit:
            end = len(data)
            start = end - 1
            while start >= 0 and (data[start] & 0xC0) == 0x80:
                start -= 1

            lead = data[start]
            width = (
                1 if lead < 0x80 else
                2 if lead < 0xE0 else
                3 if lead < 0xF0 else 4
            )
            if end - start < width:
                f.seek(-(end - start), 1)
                data = data[:start]

        number += 1
        name = f"{source.stem}.part{number:04d}.txt"
        (out / name).write_bytes(data)
        digest.update(data)

        rows.append(
            f"| [{name}]({name}) | {len(data):,} | "
            f"{offset:,} | {offset + len(data) - 1:,} |"
        )
        offset += len(data)
        print(f"{name}: {len(data):,} байт")

index = [
    f"# Части файла {source.name}",
    "",
    f"- Исходный размер: {source.stat().st_size:,} байт.",
    f"- Количество частей: {number}.",
    f"- Максимальный размер части: {limit:,} байт.",
    "- Кодировка: UTF-8; исходные байты сохранены.",
    "- Части идут в порядке номеров.",
    "- Отдельная часть не является самостоятельным XML-документом.",
    "- Граница части может проходить внутри XML-тега или строки.",
    f"- SHA-256 исходного содержимого: `{digest.hexdigest()}`.",
    "",
    "| Файл | Размер, байт | Начальный байт | Конечный байт |",
    "|---|---:|---:|---:|",
    *rows,
    "",
]

(out / "index.md").write_text("\n".join(index), encoding="utf-8")
print(f"\nГотово: {out}")