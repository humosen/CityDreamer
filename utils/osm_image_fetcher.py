# -*- coding: utf-8 -*-
#
# @File:   osm_image_fetcher.py
# @Author: Haozhe Xie
# @Date:   2023-04-04 14:46:29
# @Last Modified by: Haozhe Xie
# @Last Modified at: 2023-04-04 19:31:16
# @Email:  root@haozhexie.com

import argparse
import cv2
import logging
import numpy as np
import os
import requests
import shutil
import sys
import time

from tqdm import tqdm

OSM_TILE_SIZE = 256
PROJECT_HOME = os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir))
sys.path.append(PROJECT_HOME)

import utils.osm_helper


def get_tile_img(zoom, x, y):
    WORK_DIR = "/tmp/osm-image-fetcher"
    url = (
        "https://a.basemaps.cartocdn.com/rastertiles/voyager_nolabels/%d/%d/%d.png"
        % (zoom, x, y)
    )
    img_file_path = os.path.join(WORK_DIR, "z%d-x%d-y%d.png" % (zoom, x, y))
    os.makedirs(WORK_DIR, exist_ok=True)
    response = None
    if not os.path.exists(img_file_path):
        try:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36",
                }
            )
            response = session.get(url, stream=True)
            with open(img_file_path, "wb") as f:
                shutil.copyfileobj(response.raw, f)
        except Exception as ex:
            logging.exception(ex)
        finally:
            del response
            time.sleep(0.5)

    return cv2.imread(img_file_path)


def main(osm_dir, zoom_level):
    osm_files = sorted([f for f in os.listdir(osm_dir) if f.endswith(".osm")])
    for of in tqdm(osm_files):
        basename, _ = os.path.splitext(of)
        osm_file_path = os.path.join(osm_dir, of)
        # Create folder for the OSM
        _osm_dir = os.path.join(osm_dir, basename)
        os.makedirs(_osm_dir, exist_ok=True)
        # Read the bounds of OSM
        bounds = {
            k: float(v)
            for k, v in utils.osm_helper.get_lnglat_bounds(osm_file_path).items()
        }
        bounds = utils.osm_helper.get_nodes_xy_coordinates(
            {
                "SW": {"lng": bounds["minlon"], "lat": bounds["minlat"]},
                "NE": {"lng": bounds["maxlon"], "lat": bounds["maxlat"]},
            },
            1,
            zoom_level,
        )
        logging.debug("The bounds for OSM[Name=%s] is %s" % (of, bounds))
        # Determine the Image indexes of OpenStreetMap tiles
        sw_img_idx = {
            "x": bounds["SW"]["x"] // OSM_TILE_SIZE,
            "y": bounds["SW"]["y"] // OSM_TILE_SIZE,
        }
        ne_img_idx = {
            "x": bounds["NE"]["x"] // OSM_TILE_SIZE,
            "y": bounds["NE"]["y"] // OSM_TILE_SIZE,
        }
        logging.debug(
            "The SW image index: %s; The NE image index: %s" % (sw_img_idx, ne_img_idx)
        )
        assert (
            bounds["SW"]["x"] <= bounds["NE"]["x"]
            and bounds["SW"]["y"] >= bounds["NE"]["y"]
        )
        assert sw_img_idx["x"] <= ne_img_idx["x"] and sw_img_idx["y"] >= ne_img_idx["y"]
        # Fetch the image tiles from OpenStreetMap servers
        n_x_imgs = ne_img_idx["x"] - sw_img_idx["x"] + 1
        n_y_imgs = sw_img_idx["y"] - ne_img_idx["y"] + 1
        width = n_x_imgs * OSM_TILE_SIZE
        height = n_y_imgs * OSM_TILE_SIZE
        logging.debug("The number of images to fetch: %dx%d" % (n_x_imgs, n_y_imgs))

        osm_img = np.zeros((height, width, 3), dtype=np.uint8)
        for i in range(sw_img_idx["x"], ne_img_idx["x"] + 1):
            for j in range(ne_img_idx["y"], sw_img_idx["y"] + 1):
                offset_x = (i - sw_img_idx["x"]) * OSM_TILE_SIZE
                offset_y = (j - ne_img_idx["y"]) * OSM_TILE_SIZE
                osm_img_patch = None
                while osm_img_patch is None:
                    osm_img_patch = get_tile_img(zoom_level, i, j)

                osm_img[
                    offset_y : offset_y + OSM_TILE_SIZE,
                    offset_x : offset_x + OSM_TILE_SIZE,
                    :,
                ] = osm_img_patch
        # Crop the image
        tl_offset = {
            "x": bounds["SW"]["x"] - sw_img_idx["x"] * OSM_TILE_SIZE,
            "y": bounds["NE"]["y"] - ne_img_idx["y"] * OSM_TILE_SIZE,
        }
        br_offset = {
            "x": bounds["NE"]["x"] - (ne_img_idx["x"] + 1) * OSM_TILE_SIZE,
            "y": bounds["SW"]["y"] - (sw_img_idx["y"] + 1) * OSM_TILE_SIZE,
        }
        logging.debug("TL Offset: %s; BR Offset: %s" % (tl_offset, br_offset))
        assert (
            tl_offset["x"] < 256
            and tl_offset["x"] >= 0
            and tl_offset["y"] < 256
            and tl_offset["y"] >= 0
            and br_offset["x"] > -256
            and br_offset["x"] <= 0
            and br_offset["y"] > -256
            and br_offset["y"] <= 0
        )
        osm_img = osm_img[
            tl_offset["y"] : br_offset["y"], tl_offset["x"] : br_offset["x"]
        ]
        # Save the image
        cv2.imwrite(os.path.join(_osm_dir, "tiles.png"), osm_img)


if __name__ == "__main__":
    logging.basicConfig(
        filename=os.path.join(PROJECT_HOME, "logs", "osm-image-fetcher.log"),
        format="[%(levelname)s] %(asctime)s %(message)s",
        level=logging.DEBUG,
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--osm_dir", default=os.path.join(PROJECT_HOME, "data", "osm"))
    parser.add_argument("--zoom", default=15)
    args = parser.parse_args()
    main(args.osm_dir, args.zoom)
