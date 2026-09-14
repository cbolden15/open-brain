# Modified by Open Brain contributors for the NW0 bounded Markdown packaging proof.
"""Lazy compatibility exports; direct language imports load only that language."""
from importlib import import_module

_EXPORTS = {'extract_apex': ('graphify.extractors.apex', 'extract_apex'), 'extract_bash': ('graphify.extractors.bash', 'extract_bash'), 'extract_blade': ('graphify.extractors.blade', 'extract_blade'), 'extract_commonlisp': ('graphify.extractors.commonlisp', 'extract_commonlisp'), 'extract_dart': ('graphify.extractors.dart', 'extract_dart'), 'extract_dm': ('graphify.extractors.dm', 'extract_dm'), 'extract_dmf': ('graphify.extractors.dm', 'extract_dmf'), 'extract_dmi': ('graphify.extractors.dm', 'extract_dmi'), 'extract_dmm': ('graphify.extractors.dm', 'extract_dmm'), 'extract_elixir': ('graphify.extractors.elixir', 'extract_elixir'), 'extract_fortran': ('graphify.extractors.fortran', 'extract_fortran'), 'extract_go': ('graphify.extractors.go', 'extract_go'), 'extract_json': ('graphify.extractors.json_config', 'extract_json'), 'extract_julia': ('graphify.extractors.julia', 'extract_julia'), 'extract_markdown': ('graphify.extractors.markdown', 'extract_markdown'), 'extract_objc': ('graphify.extractors.objc', 'extract_objc'), 'extract_pascal': ('graphify.extractors.pascal', 'extract_pascal'), 'extract_delphi_form': ('graphify.extractors.pascal_forms', 'extract_delphi_form'), 'extract_lazarus_form': ('graphify.extractors.pascal_forms', 'extract_lazarus_form'), 'extract_powershell': ('graphify.extractors.powershell', 'extract_powershell'), 'extract_powershell_manifest': ('graphify.extractors.powershell', 'extract_powershell_manifest'), 'extract_razor': ('graphify.extractors.razor', 'extract_razor'), 'extract_rust': ('graphify.extractors.rust', 'extract_rust'), 'extract_sln': ('graphify.extractors.sln', 'extract_sln'), 'extract_sql': ('graphify.extractors.sql', 'extract_sql'), 'extract_terraform': ('graphify.extractors.terraform', 'extract_terraform'), 'extract_verilog': ('graphify.extractors.verilog', 'extract_verilog'), 'extract_zig': ('graphify.extractors.zig', 'extract_zig'), 'LANGUAGE_EXTRACTORS': ('graphify.extractors.registry', 'LANGUAGE_EXTRACTORS')}
__all__ = list(_EXPORTS)

def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value
