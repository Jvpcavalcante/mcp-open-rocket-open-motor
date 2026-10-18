"""Baixa as dependências externas fixadas para vendor/ (funciona em Windows, Linux e macOS).
Não precisa de git nem de compilador."""
import io
import shutil
import urllib.request
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
VENDOR = RAIZ / "vendor"

OM_COMMIT = "dcc7fd62045d55036451b833f60e16a786f4685d"  # openMotor 0.6.2 (26/09/2026), versão analisada
OM_URL = f"https://codeload.github.com/reilleya/openMotor/zip/{OM_COMMIT}"
OR_VERSION = "24.12"
OR_URL = (f"https://github.com/openrocket/openrocket/releases/download/"
          f"release-{OR_VERSION}/OpenRocket-{OR_VERSION}.jar")


def baixar(url: str) -> bytes:
    print("Baixando", url)
    with urllib.request.urlopen(url) as r:
        return r.read()


def main() -> None:
    VENDOR.mkdir(exist_ok=True)
    (RAIZ / "motores").mkdir(exist_ok=True)

    destino = VENDOR / "openMotor"
    if not destino.exists():
        with zipfile.ZipFile(io.BytesIO(baixar(OM_URL))) as z:
            z.extractall(VENDOR)
        shutil.move(str(VENDOR / f"openMotor-{OM_COMMIT}"), destino)

    jar = VENDOR / f"OpenRocket-{OR_VERSION}.jar"
    if not jar.exists():
        jar.write_bytes(baixar(OR_URL))
    print("vendor/ pronto:", *sorted(p.name for p in VENDOR.iterdir()))


if __name__ == "__main__":
    main()
