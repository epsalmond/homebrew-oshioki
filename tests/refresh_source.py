"""Load the literal helper that Homebrew writes during post-install."""
import pathlib
import re
import types


def load():
    formula = pathlib.Path(__file__).resolve().parents[1] / "Formula/oshioki.rb"
    source = re.search(r"<<~'PYTHON', base: :libexec\n(.*?)^    PYTHON$", formula.read_text(), re.M | re.S)[1]
    source = "\n".join(line[6:] for line in source.splitlines())
    module = types.ModuleType("refresh_agent")
    exec(compile(source, str(formula), "exec"), module.__dict__)
    return module
