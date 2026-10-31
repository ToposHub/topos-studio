"""vault 测试包。

每个测试模块用独立的临时 vault 目录（通过 TOPOS_VAULT_DIR 环境变量隔离），
不污染用户真实数据。见 conftest.py 的 vault_dir fixture。
"""
