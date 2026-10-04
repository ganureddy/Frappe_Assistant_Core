# Shim for the older bench/pip/setuptools versions commonly found on Frappe v13
# servers, which need setup.py for editable installs (`pip install -e`) and may
# not understand PEP 621 metadata. pyproject.toml remains the source of truth:
# the version and dependency list are read from the package and pyproject.toml.
import re

from setuptools import find_packages, setup

with open("frappe_assistant_core/__init__.py") as f:
    version = re.search(r'^__version__ = ["\']([^"\']+)["\']', f.read(), re.M).group(1)

with open("pyproject.toml") as f:
    pyproject = f.read()

dependencies_block = re.search(r"^dependencies = \[(.*?)^\]", pyproject, re.M | re.S).group(1)
install_requires = re.findall(r'^\s*"([^"]+)"', dependencies_block, re.M)

setup(
    name="frappe_assistant_core",
    version=version,
    description="AI-powered ERP assistant with comprehensive MCP integration for Frappe framework",
    author="Paul Clinton",
    author_email="jypaulclinton@gmail.com",
    license="AGPL-3.0-or-later",
    python_requires=">=3.8",
    packages=find_packages(include=["frappe_assistant_core", "frappe_assistant_core.*"]),
    include_package_data=True,
    zip_safe=False,
    install_requires=install_requires,
)
