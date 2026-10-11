"""Controlled agents honor the same durable output contract as real providers."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import re
import runpy
import shutil
from uuid import uuid4


def claim(test, execution, reference):
    if execution.delivery_observer:
        execution.delivery_observer(reference, str(uuid4()), execution.handoff)
        match = re.search(r"(/[^ \n']*/task-loop-[^ /\n']+)/complete.py", execution.handoff)
        if match:
            test.addCleanup(shutil.rmtree, Path(match[1]), True)


def complete_prompt(prompt):
    match = re.search(r"(/[^ \n']*/task-loop-[^ /\n']+)/complete.py", prompt)
    if match:
        path = Path(match[1])
        if (path / "result.json").exists():
            try:
                with redirect_stdout(io.StringIO()) as output:
                    runpy.run_path(str(path / "complete.py"))
                return output.getvalue().strip()
            except (ValueError, KeyError):
                pass  # Deliberately malformed fixture output remains unsealed.


def complete(execution):
    if execution.delivery_observer:
        return complete_prompt(execution.handoff)
