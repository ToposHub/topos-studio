"""
Test nodes conftest - pytest fixtures for node tests.

Provides common fixtures used across all node tests.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


@pytest.fixture
def mock_ctx():
    """Mock ModernGL context fixture."""
    from tests.node_test_base import MockModernGLContext
    return MockModernGLContext()


@pytest.fixture
def mock_fb_manager(mock_ctx):
    """Mock framebuffer manager fixture."""
    from tests.node_test_base import MockFramebufferManager
    return MockFramebufferManager(mock_ctx)


@pytest.fixture
def mock_resource_pool(mock_fb_manager):
    """Mock GPU resource pool fixture."""
    from tests.node_test_base import MockGPUResourcePool
    return MockGPUResourcePool(mock_fb_manager)


@pytest.fixture
def valid_texture_result():
    """Valid TextureResult fixture."""
    from tests.node_test_base import make_texture_result
    return make_texture_result()


@pytest.fixture
def valid_inputs(valid_texture_result):
    """Valid inputs dict fixture."""
    return {"image": valid_texture_result}