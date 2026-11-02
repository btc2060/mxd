from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from lionbot.garden_vision import GardenVision
from tests.test_garden import scene,SKY


class GardenPositionTests(unittest.TestCase):
    def test_old_nameplate_offset_cannot_override_manual_feet(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);frame,c=scene(root)
            vision=GardenVision(replace(c,player_feet_offset=(100,-10)),root)
            with patch.object(vision.locator,'locate',return_value=(380,300)) as nameplate,patch.object(vision.mapper,'seed') as seed:
                obs=vision.observe(frame,now=0)
            self.assertAlmostEqual(obs.player_feet[0],312,delta=2)
            self.assertEqual(obs.location_source,'terrain')
            nameplate.assert_not_called();seed.assert_not_called()

    def test_fixed_reference_error_does_not_accumulate_after_knockback(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);frame,c=scene(root);vision=GardenVision(c,root)
            original=[(r.position,r.feet) for r in vision.fixed_references]
            y=vision.fixed_references[0].position[1]
            with patch.object(vision.mapper,'translation',return_value=np.array([.5,0.])):
                for i in range(60):
                    x=.3+(.02 if i%2 else -.02)
                    feet,source=vision.locate_stationary(frame,(x,y),i*.1)
                    self.assertAlmostEqual(feet[0],312+(x-.3)*c.world_pixels_per_x+.5,delta=.001)
            self.assertEqual([(r.position,r.feet) for r in vision.fixed_references],original)
            self.assertEqual([(r.position,r.feet) for r in vision.mapper.references],original)

    def test_knockback_and_return_relocalize_without_changing_reach_center(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);frame,c=scene(root);hit=frame.copy()
            hit[4:74,4:144]=30;cv2.rectangle(hit,(41,55),(45,59),(0,255,255),-1)
            hit[310:324,280:344]=SKY
            for reach in (172,270):
                vision=GardenVision(replace(c,attack_range_px=reach),root)
                after=vision.observe(hit,now=0)
                self.assertAlmostEqual(after.player_feet[0],312-3/140*c.world_pixels_per_x,delta=2)
                returned=vision.observe(frame,now=.1)
                self.assertAlmostEqual(returned.player_feet[0],312,delta=2)
                with patch('lionbot.vision.cv2.rectangle',wraps=cv2.rectangle) as rectangles:
                    vision.draw(frame,returned,'test','right')
                band=next(item for item in rectangles.call_args_list if item.args[3]==(255,0,255))
                left,right=band.args[1][0],band.args[2][0]
                self.assertEqual(right-left,2*reach)
                self.assertAlmostEqual((left+right)/2,returned.player_feet[0],delta=1)
