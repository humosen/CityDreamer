# -*- coding: utf-8 -*-
#
# @File:   dataset_generator.py
# @Author: Haozhe Xie
# @Date:   2023-03-31 15:04:25
# @Last Modified by: Haozhe Xie
# @Last Modified at: 2023-04-18 15:54:29
# @Email:  root@haozhexie.com

import argparse
import cv2
import json
import logging
import matplotlib.pyplot as plt
import math
import numpy as np
import os
import sys
import torch

from tqdm import tqdm
from PIL import Image

PROJECT_HOME = os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir))
sys.path.append(PROJECT_HOME)

import utils.helpers
import utils.osm_helper
import extensions.voxlib as voxlib
from extensions.extrude_tensor import TensorExtruder


def _tag_equals(tags, key, values=None):
    if key not in tags:
        return False
    if values is None:
        return True
    return tags[key] in values


def _get_highway_color(map_name, highway_tags):
    if map_name == "height_field":
        return 0
    elif map_name == "seg_map":
        # Ignore underground highways
        # return 0 if "layer" in highway_tags and highway_tags["layer"] < 0 else 1
        return 1
    else:
        raise Exception("Unknown map name: %s" % map_name)


def _get_footprint_color(map_name, footprint_tags):
    if map_name == "height_field":
        if _tag_equals(footprint_tags, "role", ["inner"]):
            # "role" in footprint_tags and footprint_tags["role"] == "inner"
            return None
        elif _tag_equals(footprint_tags, "building", ["roof"]):
            #  "building" in footprint_tags and footprint_tags["building"] == "roof"
            return None
        elif _tag_equals(footprint_tags, "landuse", ["construction"]):
            #  "building" in footprint_tags and footprint_tags["building"] == "construction"
            return 10
        else:
            assert "height" in footprint_tags
            return int(float(footprint_tags["height"]) + 0.5)
    elif map_name == "seg_map":
        if _tag_equals(footprint_tags, "role", ["inner"]):
            return 2
        elif _tag_equals(footprint_tags, "building"):
            return 2
        elif _tag_equals(footprint_tags, "landuse", ["construction"]):
            return 4
        else:
            return 0
    elif map_name == "footprint_contour":
        if _tag_equals(footprint_tags, "building"):
            return 1
        else:
            return 0
    else:
        raise Exception("Unknown map name: %s" % map_name)


def _remove_mask_outliers(mask):
    N_PIXELS_THRES = 96
    mask = cv2.erode(mask, np.ones((5, 5), dtype=np.uint8))
    _, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
    ignored_indexes = np.where(stats[:, -1] <= N_PIXELS_THRES)[0]
    ignored_mask = np.isin(labels, ignored_indexes)
    return mask * ~ignored_mask


def get_coast_zones(osm_tile_img_path, img_size):
    tile_img = cv2.imread(osm_tile_img_path)
    water_lb = np.array([219, 219, 195], dtype=np.uint8)
    water_ub = np.array([235, 235, 219], dtype=np.uint8)
    mask = cv2.resize(cv2.inRange(tile_img, water_lb, water_ub), img_size[::-1])
    return _remove_mask_outliers(mask)


def get_green_lands(osm_tile_img_path, seg_map):
    tile_img = cv2.imread(osm_tile_img_path)
    green_lb = np.array([209, 217, 217], dtype=np.uint8)
    green_ub = np.array([219, 236, 233], dtype=np.uint8)
    # Only assign green lands to uncategoried pixels
    uncategoried = np.zeros_like(seg_map)
    uncategoried[seg_map == 0] = True
    mask = cv2.resize(cv2.inRange(tile_img, green_lb, green_ub), seg_map.shape[::-1])
    return _remove_mask_outliers(mask) * uncategoried


def get_osm_images(osm_file_path, osm_tile_img_path, zoom_level):
    logging.debug("Reading OSM file[Path=%s] ..." % osm_file_path)
    highways, footprints, nodes = utils.osm_helper.get_highways_and_footprints(
        osm_file_path
    )
    resolution = utils.osm_helper.get_map_resolution(
        {
            k: float(v)
            for k, v in utils.osm_helper.get_lnglat_bounds(osm_file_path).items()
        },
        zoom_level,
    )
    nodes = utils.osm_helper.get_nodes_xy_coordinates(nodes, resolution, zoom_level)
    xy_bounds = utils.osm_helper.get_xy_bounds(nodes)

    # Fix missing height (for buildings) and width for highways
    highways = utils.osm_helper.fix_missing_highway_width(highways)
    footprints = utils.osm_helper.fix_missing_footprint_height(
        footprints, utils.osm_helper.get_footprint_height_stat(footprints)
    )

    # Generate semantic labels
    logging.debug("Generating segmentation maps ...")
    seg_map = utils.osm_helper.get_empty_map(xy_bounds)
    seg_map = utils.osm_helper.plot_highways(
        "seg_map", _get_highway_color, seg_map, highways, nodes, xy_bounds, resolution
    )
    # Read coast zones from no-label tile images
    logging.debug("Reading green lands and coast zones from tile images ...")
    coast_zones = None
    green_lands = None
    if not os.path.exists(osm_tile_img_path):
        logging.warning(
            "The coast zones for the OSM[Path=%s] could not be parsed due to a missing tile image[Path=%s]"
            % (osm_file_path, osm_tile_img_path)
        )
    else:
        green_lands = get_green_lands(osm_tile_img_path, seg_map)
        seg_map[green_lands != 0] = 3
        coast_zones = get_coast_zones(osm_tile_img_path, seg_map.shape)
        seg_map[coast_zones != 0] = 5
    # Plot footprint at the end to make building masks more complete
    seg_map = utils.osm_helper.plot_footprints(
        "seg_map",
        _get_footprint_color,
        seg_map,
        footprints,
        nodes,
        xy_bounds,
    )
    # Assign ID=6 to unlabelled pixels (regarded as ground)
    seg_map[seg_map == 0] = 6

    # Generate the contours of footprints
    logging.debug("Generating footprint contours ...")
    footprint_contour = utils.osm_helper.get_empty_map(xy_bounds)
    footprint_contour = utils.osm_helper.plot_footprints(
        "footprint_contour",
        _get_footprint_color,
        footprint_contour,
        footprints,
        nodes,
        xy_bounds,
        resolution,
    )

    # Generate height fields
    logging.debug("Generating height fields ...")
    height_field = utils.osm_helper.get_empty_map(xy_bounds, dtype=np.uint16)
    height_field = utils.osm_helper.plot_highways(
        "height_field",
        _get_highway_color,
        height_field,
        highways,
        nodes,
        xy_bounds,
        resolution,
    )
    height_field[height_field == 0] = 4
    if coast_zones is not None:
        height_field[coast_zones != 0] = 0
    if green_lands is not None:
        height_field[green_lands != 0] = 8
    # Follow the order in plotting seg maps
    height_field = utils.osm_helper.plot_footprints(
        "height_field",
        _get_footprint_color,
        height_field,
        footprints,
        nodes,
        xy_bounds,
    )

    # The height values should be normalized using the same scale as that of the width and height 
    # dimensions. However, the following statement results in incorrect outputs. Quite STRANGE!
    # height_field = (height_field / resolution).astype(np.uint16)
    height_field = height_field.astype(np.uint16)
    return (
        height_field,
        seg_map,
        footprint_contour,
        {"resolution": resolution, "bounds": xy_bounds},
    )


def get_google_earth_projects(osm_basename, google_earth_dir):
    ge_projects = os.listdir(google_earth_dir)
    osm_info = osm_basename.split("-")
    osm_country, osm_city = osm_info[0], osm_info[1]
    projects = []
    for gp in ge_projects:
        if gp.startswith("%s-%s" % (osm_country, osm_city)):
            projects.append(gp)

    return projects


def get_google_earth_camera_poses(ge_proj_name, ge_dir):
    ge_proj_dir = os.path.join(ge_dir, ge_proj_name)
    camera_setting_file = os.path.join(ge_proj_dir, "%s.json" % ge_proj_name)
    ge_project_file = os.path.join(ge_proj_dir, "%s.esp" % ge_proj_name)
    if not os.path.exists(camera_setting_file) or not os.path.exists(ge_project_file):
        return None

    camera_settings = None
    with open(camera_setting_file) as f:
        camera_settings = json.loads(f.read())

    camera_target = None
    with open(ge_project_file) as f:
        ge_proj_settings = json.loads(f.read())
        scene = ge_proj_settings["scenes"][0]["attributes"]
        camera_group = next(
            _attr["attributes"] for _attr in scene if _attr["type"] == "cameraGroup"
        )
        camera_taget_effect = next(
            _attr["attributes"]
            for _attr in camera_group
            if _attr["type"] == "cameraTargetEffect"
        )
        camera_target = next(
            _attr["attributes"]
            for _attr in camera_taget_effect
            if _attr["type"] == "poi"
        )

    camera_poses = {
        "vfov": camera_settings["cameraFrames"][0]["fovVertical"],
        "width": camera_settings["width"],
        "height": camera_settings["height"],
        "center": {
            "coordinate": {
                "longitude": next(
                    _attr["value"]["relative"]
                    for _attr in camera_target
                    if _attr["type"] == "longitudePOI"
                )
                * 360
                - 180,
                "latitude": next(
                    _attr["value"]["relative"]
                    for _attr in camera_target
                    if _attr["type"] == "latitudePOI"
                )
                * 180
                - 90,
                "altitude": next(
                    _attr["value"]["relative"]
                    for _attr in camera_target
                    if _attr["type"] == "altitudePOI"
                )
                + 1,
            }
        },
        "poses": [],
    }
    # NOTE: All Google Earth renderings are centered around an altitude of 1.
    assert camera_poses["center"]["coordinate"]["altitude"] == 1
    for cf in camera_settings["cameraFrames"]:
        camera_poses["poses"].append(
            # Note: Rotation is no longer needed now
            # {"rotation": cf["rotation"], "coordinate": cf["coordinate"]}
            {"coordinate": cf["coordinate"]}
        )
    return camera_poses


def _get_diffuse_shading_img(seg_map, depth2, raydirs, cam_ori_t):
    mc_rgb = np.array(seg_map.convert("RGB"))
    # Diffused shading, co-located light.
    first_intersection_depth = depth2[0, :, :, 0, None, :]
    first_intersection_point = (
        raydirs * first_intersection_depth + cam_ori_t[None, None, None, :]
    )
    fip_local_coords = torch.remainder(first_intersection_point, 1.0)
    fip_wall_proximity = torch.minimum(fip_local_coords, 1.0 - fip_local_coords)
    fip_wall_orientation = torch.argmin(fip_wall_proximity, dim=-1, keepdim=False)
    # 0: [1,0,0]; 1: [0,1,0]; 2: [0,0,1]
    lut = torch.tensor(
        [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        dtype=torch.float32,
        device=fip_wall_orientation.device,
    )
    fip_normal = lut[fip_wall_orientation]
    diffuse_shade = torch.abs(torch.sum(fip_normal * raydirs, dim=-1))

    mc_rgb = mc_rgb.astype(float) / 255
    mc_rgb = mc_rgb * diffuse_shade.cpu().numpy()
    mc_rgb = (mc_rgb ** (1 / 2.2)) * 255
    return Image.fromarray(mc_rgb.astype(np.uint8))


def get_google_earth_aligned_seg_maps(
    ge_project_name,
    google_earth_dir,
    seg_map,
    height_field,
    patch_size,
    metadata,
    zoom_level,
    tensor_extruder,
):
    ge_camera_poses = get_google_earth_camera_poses(ge_project_name, google_earth_dir)
    ge_camera_focal = (
        ge_camera_poses["height"] / 2 / np.tan(np.deg2rad(ge_camera_poses["vfov"]))
    )
    ## Build semantic 3D volume
    logging.debug("Camera Target Center: %s" % ge_camera_poses["center"]["coordinate"])
    cx, cy = utils.osm_helper.lnglat2xy(
        ge_camera_poses["center"]["coordinate"]["longitude"],
        ge_camera_poses["center"]["coordinate"]["latitude"],
        metadata["resolution"],
        zoom_level,
    )
    ge_camera_poses["center"]["position"] = {
        "x": cx - metadata["bounds"]["xmin"],
        "y": cy - metadata["bounds"]["ymin"],
        "z": ge_camera_poses["center"]["coordinate"]["altitude"]
    }
    logging.debug(
        "Map Information: Center=%s; Size(HxW): %s"
        % (
            ge_camera_poses["center"]["position"],
            (
                metadata["bounds"]["ymax"] - metadata["bounds"]["ymin"],
                metadata["bounds"]["xmax"] - metadata["bounds"]["xmin"],
            ),
        )
    )
    part_seg_map = _get_img_patch(
        seg_map,
        ge_camera_poses["center"]["position"]["x"],
        ge_camera_poses["center"]["position"]["y"],
        patch_size,
    )
    part_hf = _get_img_patch(
        height_field,
        ge_camera_poses["center"]["position"]["x"],
        ge_camera_poses["center"]["position"]["y"],
        patch_size,
    )
    seg_volume = tensor_extruder(
        torch.from_numpy(part_seg_map[None, None, ...]).cuda(),
        torch.from_numpy(part_hf[None, None, ...]).cuda(),
    ).squeeze()
    logging.debug("The shape of SegVolume: %s" % (seg_volume.size(),))
    ## Convert camera position to the voxel coordinate system
    vol_cx, vol_cy, vol_cz = ((patch_size - 1) // 2, (patch_size - 1) // 2, 0)

    # Fix Google Earth STRANGE offset
    offset = {"x": 0, "y": 0}
    for gcp in ge_camera_poses["poses"]:
        x, y = utils.osm_helper.lnglat2xy(
            gcp["coordinate"]["longitude"],
            gcp["coordinate"]["latitude"],
            metadata["resolution"],
            zoom_level,
            dtype=float,
        )
        gcp["position"] = {
            "x": x - cx,
            "y": y - cy,
            "z": gcp["coordinate"]["altitude"]
        }
    offset["x"] = np.mean([gcp["position"]["x"] for gcp in ge_camera_poses["poses"]])
    offset["y"] = np.mean([gcp["position"]["y"] for gcp in ge_camera_poses["poses"]])
    logging.debug("XY Coordinates Offset: %s", offset)

    seg_maps = []
    for gcp in tqdm(ge_camera_poses["poses"], desc="Project: %s" % ge_project_name):
        gcp["position"]["x"] -= offset["x"] - vol_cx
        gcp["position"]["y"] -= offset["y"] - vol_cy
        # logging.debug("Camera parameters: %s" % gcp)
        ## Run ray-voxel intersection
        r"""Ray-voxel intersection CUDA kernel.
        Note: voxel_id = 0 and depth2 = NaN if there is no intersection along the ray
        Args:
            voxel_t (H x W x D tensor, int32): Full 3D voxel of MC block IDs.
            cam_ori_t (3 tensor): Camera origin.
            cam_dir_t (3 tensor): Camera direction.
            cam_up_t (3 tensor): Camera up vector.
            cam_f (float): Camera focal length (in pixels).
            cam_c  (list of 2 floats [x, y]): Camera optical center.
            img_dims (list of 2 ints [H, W]): Camera resolution.
            max_samples (int): Maximum number of blocks intersected along the ray before stopping.
        Returns:
            voxel_id (    img_dims[0] x img_dims[1] x max_samples x 1 tensor): IDs of intersected tensors
            along each ray
            depth2   (2 x img_dims[0] x img_dims[1] x max_samples x 1 tensor): Depths of entrance and exit
            points for each ray-voxel intersection.
            raydirs  (    img_dims[0] x img_dims[1] x 1 x 3 tensor): The direction of each ray.

        """
        N_MAX_SAMPLES = 6
        cam_ori_t = torch.tensor(
            [gcp["position"]["y"], gcp["position"]["x"], gcp["position"]["z"]],
            dtype=torch.float32,
            device=seg_volume.device,
        )
        voxel_id, depth2, raydirs = voxlib.ray_voxel_intersection_perspective(
            seg_volume,
            cam_ori_t,
            torch.tensor(
                [
                    vol_cy - gcp["position"]["y"],
                    vol_cx - gcp["position"]["x"],
                    vol_cz - gcp["position"]["z"],
                ],
                dtype=torch.float32,
                device=seg_volume.device,
            ),
            torch.tensor([0, 0, 1], dtype=torch.float32),
            # MAGIC NUMBER 2 to make it aligned with Google Earth Renderings
            ge_camera_focal * 2,
            [
                (ge_camera_poses["height"] - 1) / 2.0,
                (ge_camera_poses["width"] - 1) / 2.0,
            ],
            [ge_camera_poses["height"], ge_camera_poses["width"]],
            N_MAX_SAMPLES,
        )
        # print(voxel_id.size())    # torch.Size([540, 960, 10, 1])
        seg_map = utils.helpers.get_seg_map(voxel_id.squeeze()[..., 0].cpu().numpy())
        seg_maps.append(seg_map)
        # seg_maps.append(_get_diffuse_shading_img(seg_map))

    return seg_maps


def _get_img_patch(img, cx, cy, patch_size):
    h, w = img.shape
    x_s, x_e = cx - patch_size // 2, cx + patch_size // 2
    h_s, h_e = cy - patch_size // 2, cy + patch_size // 2
    if x_s < 0 or x_e >= w:
        x_s = 0 if x_s < 0 else x_s
        x_e = 0 if x_e >= w else x_e
        logging.error("The horizontal center is not located at %d as expected" % cx)
    if h_s < 0 or h_e >= h:
        h_s = 0 if h_s < 0 else h_s
        h_e = 0 if h_e >= h else h_e
        logging.error("The vertical center is not located at %d as expected" % cy)
    return img[h_s:h_e, x_s:x_e].astype(np.int32)


def main(osm_dir, google_earth_dir, output_dir, patch_size, max_height, zoom_level):
    osm_files = sorted([f for f in os.listdir(osm_dir) if f.endswith(".osm")])
    tensor_extruder = TensorExtruder(max_height)
    for of in tqdm(osm_files):
        basename, _ = os.path.splitext(of)
        # Create folder for the OSM
        _output_dir = os.path.join(output_dir, basename)
        os.makedirs(_output_dir, exist_ok=True)
        # Rasterisation
        height_field, seg_map, contours, metadata = get_osm_images(
            os.path.join(osm_dir, of),
            os.path.join(_output_dir, "tiles.png"),
            zoom_level,
        )
        Image.fromarray(height_field).save(os.path.join(_output_dir, "hf.png"))
        Image.fromarray(contours.astype(bool)).save(
            os.path.join(_output_dir, "ctr.png")
        )
        utils.helpers.get_seg_map(seg_map).save(os.path.join(_output_dir, "seg.png"))
        # Align images from Google Earth Studio
        logging.debug("Generating Google Earth segmentation maps ...")
        ge_projects = [] # get_google_earth_projects(basename, google_earth_dir)
        if not ge_projects:
            logging.warning(
                "No matching Google Earth Project found for OSM[File=%s]." % of
            )
            continue
        ## Read Google Earth Studio metadata
        for gep in ge_projects:
            seg_maps = get_google_earth_aligned_seg_maps(
                gep,
                google_earth_dir,
                seg_map,
                height_field,
                patch_size,
                metadata,
                zoom_level,
                tensor_extruder,
            )
            # Generate the corresponding segmentation images
            ges_seg_dir = os.path.join(google_earth_dir, gep, "seg")
            os.makedirs(ges_seg_dir, exist_ok=True)
            for idx, sg in enumerate(seg_maps):
                sg.save(os.path.join(ges_seg_dir, "%s-%04d.png" % (gep, idx)))


if __name__ == "__main__":
    plt.rcParams["figure.figsize"] = (20, 20)
    logging.basicConfig(
        filename=os.path.join(PROJECT_HOME, "output", "dataset-generator.log"),
        format="[%(levelname)s] %(asctime)s %(message)s",
        level=logging.DEBUG,
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--osm_dir", default=os.path.join(PROJECT_HOME, "data", "xml"))
    parser.add_argument("--ges_dir", default=os.path.join(PROJECT_HOME, "data", "ges"))
    parser.add_argument(
        "--output_dir", default=os.path.join(PROJECT_HOME, "data", "osm")
    )
    parser.add_argument("--patch_size", default=1536)
    parser.add_argument("--max_height", default=640)
    parser.add_argument("--zoom", default=18)
    args = parser.parse_args()
    main(
        args.osm_dir,
        args.ges_dir,
        args.output_dir,
        args.patch_size,
        args.max_height,
        args.zoom,
    )
