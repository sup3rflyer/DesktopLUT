"""Heavy colour-engine modules for DLC v2 (numpy / scipy / colour-science).

Containment boundary
--------------------
The DLC *spine* — ``stage.py``, ``controller.py``, ``refine.py``, the stage
tools, the named-pipe contract — is deliberately dependency-free so the
arbitrating assistant and the C++ controller path never need a scientific
Python stack. Everything in *this* package may import numpy / scipy /
``colour``.

To preserve that boundary this ``__init__`` imports **nothing** from its
submodules: ``import dlc`` and ``import dlc.controller`` must not transitively
pull numpy. Import the specific engine module you need instead::

    from dlc.engine import patches              # numpy-free (pure stdlib)
    from dlc.engine.model import DisplayErrorModel
    from dlc.engine.lut_rbf import build_cube
    from dlc.engine.lut_sdr import build_sdr_cube
    from dlc.engine import whitepoint

Install the engine dependencies with::

    pip install -r requirements.txt        # or:  pip install -e .[engine]

These recipes are ported from the sibling ColorCalibration lab
(``generate_patches.py`` / ``generate_lut.py`` / ``generate_sdr_lut.py``) and
decoupled from the ColourSpace ``.bcs`` format: every builder consumes DLC's own
per-patch measurements (numpy arrays) rather than parsing a measurement file.
"""

__all__: list[str] = []
