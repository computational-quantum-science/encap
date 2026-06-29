import os
import yaml
import pytest
from encap_lib import encap_settings as settings

def test_load_encap_config_files_recursive(tmp_path):
    # Setup a nested directory structure with .encap.conf files
    # tmp_path
    # ├── .encap.conf (a: 1, b: 1)
    # └── subdir
    #     ├── .encap.conf (b: 2, c: 3)
    #     └── deeper
    #         └── .encap.conf (c: 4)
    
    # Save the original config to restore later
    original_config = settings.config.copy()
    original_args_config = settings.args_config.copy()
    
    try:
        settings.config = {}
        settings.args_config = {}
        
        # Root config
        (tmp_path / ".encap.conf").write_text(yaml.dump({"a": 1, "b": 1}))
        
        # Subdir config
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / ".encap.conf").write_text(yaml.dump({"b": 2, "c": 3}))
        
        # Deeper config
        deeper = subdir / "deeper"
        deeper.mkdir()
        (deeper / ".encap.conf").write_text(yaml.dump({"c": 4}))
        
        # Call recursive load on the deepest folder
        settings.load_encap_config_files_recursive(str(deeper))
        
        # Config should have merged values, closer to the target overrides parents
        # deeper overrides subdir overrides root
        # Result: a=1, b=2, c=4
        assert settings.config.get("a") == 1
        assert settings.config.get("b") == 2
        assert settings.config.get("c") == 4
        
    finally:
        # Restore
        settings.config = original_config
        settings.args_config = original_args_config
