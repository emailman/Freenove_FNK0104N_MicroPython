# display_driver_framework.DisplayDriver.init() picks up the init-command-table module by
# name -- f'_{self.__class__.__name__.lower()}_init' (see display_driver_framework.py) -- so
# ST77922Landscape needs its own '_st77922landscape_init' module, distinct from ST77922's
# '_st77922_init', purely because of that naming convention. The actual init sequence is
# identical: ST77922Landscape never touches MADCTL (see its docstring in st77922.py), so the
# panel is initialized exactly the same way regardless of which class is driving it. Re-export
# rather than duplicate the table.
#
# DisplayDriver.init() does `del sys.modules[mod_name]` after calling mod.init(self), to free
# the (one-shot, run-once) init table's memory -- since mod_name here is this shim, not
# '_st77922_init' itself, drop the real module from sys.modules ourselves so that cleanup still
# happens for the actual table, not just this thin wrapper.
import sys
from _st77922_init import init  # NOQA

if '_st77922_init' in sys.modules:
    del sys.modules['_st77922_init']
