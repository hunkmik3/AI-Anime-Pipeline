import numpy as np
import pytest


@pytest.mark.parametrize('shape', [(640,360,3), (1080,1920,3), (17,23,3)])
def test_resizing_before_rgb_swap_preserves_transnet_pixels_exactly(shape):
    import cv2
    frame=np.random.default_rng(42).integers(0,256,shape,dtype=np.uint8)
    prior=cv2.resize(frame[:,:,::-1],(48,27),interpolation=cv2.INTER_AREA)
    fast=cv2.resize(frame,(48,27),interpolation=cv2.INTER_AREA)[:,:,::-1]
    np.testing.assert_array_equal(fast,prior)
