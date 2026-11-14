from . import controllers
from . import models
from . import wizard


def _post_init_seed_demo(env):
    """Fresh install: give the demo discharge its note, medication and supplies (see DEMO_* constants)."""
    discharge = env.ref('clearpath_discharge.demo_discharge_pt402', raise_if_not_found=False)
    if discharge and not discharge.medication_line_ids and not discharge.supply_line_ids:
        discharge._seed_demo_content()
