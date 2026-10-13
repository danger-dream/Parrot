"""Install a checked, maintainer-built native library without Go or a C compiler."""
import importlib.util
import json
from pathlib import Path
import shutil

from setuptools import Distribution, find_packages, setup
from setuptools.command.build_py import build_py
from wheel.bdist_wheel import bdist_wheel

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("native_artifacts", ROOT / "native_artifacts.py")
artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifacts)


class Build(build_py):
    def run(self):
        arch = artifacts.current_arch()
        manifest = artifacts.verify(ROOT, arch)
        super().run()
        target = Path(self.build_lib).resolve() / "parrot_go_tls_client"
        target.mkdir(parents=True, exist_ok=True)
        record = manifest["artifacts"][arch]
        shutil.copyfile(ROOT / record["path"], target / "libparrot_go_tls_client.so")
        identity = {"schema": 1, "version": manifest["version"],
                    "source_sha256": manifest["source_sha256"], "arch": arch,
                    "sha256": record["sha256"], "glibc_min": record["glibc_min"]}
        (target / "native_build.json").write_text(json.dumps(identity, sort_keys=True) + "\n")


class BinaryDistribution(Distribution):
    def has_ext_modules(self):
        return True


class Wheel(bdist_wheel):
    def get_tag(self):
        _python, _abi, platform = super().get_tag()
        return "py3", "none", platform


setup(name="parrot-go-tls-client", version="0.2.1", python_requires=">=3.10",
      description="Cancellable LS 2.22.0 TLS/h2 transport primitives",
      packages=find_packages("python"), package_dir={"": "python"},
      include_package_data=False, cmdclass={"build_py": Build, "bdist_wheel": Wheel},
      distclass=BinaryDistribution)
