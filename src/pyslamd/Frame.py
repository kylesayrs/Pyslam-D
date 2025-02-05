from typing import Tuple, List, Optional

import cv2
import copy
import numpy
import pymap3d
import open3d as o3d
from PIL import Image

from pyslamd.Settings import Settings, CameraSettings
from pyslamd.utils.exif import get_exif_measurements
from pyslamd.utils.pose import (
    orientation_to_rotation,
    get_rotation,
    set_rotation,
    get_translation,
    set_translation,
    get_pose
)


class Frame:
    """
    Frame object for logically grouping gps, keypoints, pose, and point clouds.
    A frame object that is assigned a key_frame_num is a keyframe

    :param image_path: path to frame image data
    :param frame_num: frame number, assigned sequentially
    :param settings: settings used for camera intrinsics and cloud sparsity
    """
    def __init__(
        self,
        image_path: str,
        frame_num: int,
        settings: Settings,
    ):
        self.image_path = image_path
        self.frame_num = frame_num
        self.key_frame_num = None
        self.settings = settings

        # read image
        pil_image = Image.open(image_path)
        self.image = numpy.asarray(pil_image)
        self.gps_coords, self.imu_orientation = get_exif_measurements(pil_image, settings)

        self.keypoints = None
        self.descriptors = None
        
        self.global_pose = None

        self.world_point_cloud_cache = None
        self.global_point_cloud_cache = None
        self.point_cloud_needs_add = True

        self.gps_imu_extrinsic_cache = None


    def set_key_frame_num(self, key_frame_num: int):
        """
        :param key_frame_num: key frame number to assign to frame
        """
        self.key_frame_num = key_frame_num


    def set_global_pose(self, pose: numpy.ndarray):
        """
        :param pose: 4x4 pose relative to the first key frame
        """
        self.global_pose = pose
        self.global_point_cloud_cache = None

    
    def get_image(self) -> numpy.ndarray:
        return self.image


    def assign_keypoints(self, keypoints: List[cv2.KeyPoint], descriptors: numpy.ndarray):
        """
        Keypoints are calculated and assigned by the FrameMatcher

        :param keypoints: list of keypoint positions
        :param descriptors: list of keypoint descriptors
        """
        self.keypoints = keypoints
        self.descriptors = descriptors

    
    def image_to_world_point(
        self,
        x_position: float,
        y_position: float
    ) -> Tuple[float, float, float]:
        """
        Apply inverse camera intrinsic operation to map an image point to a
        world point

        Image points are wrt the top left corner

        The inverse camera intrinsic matrix is
        [[depth / fx      0        -cx * depth / fx]
         [     0      depth / fy   -cy * depth / fy]
         [     0          0            depth       ]]

        :param x_position: x position of point
        :param y_position: y position of point
        :return: east-north-down position of the point in the world
        """
        depth = self._get_pixel_depth(x_position, y_position)

        y_position = self.settings.camera.height - y_position  # convert from TL to BL

        fx = self.settings.camera.fx
        fy = self.settings.camera.fy
        cx = self.settings.camera.cx
        cy = self.settings.camera.cy

        return numpy.array([
            (x_position - cx) * depth / fx,
            (y_position - cy) * depth / fy,
            depth
        ])


    def get_point_cloud(self) -> o3d.geometry.PointCloud:
        """
        The global point cloud cache is cleared whenever the frame is
        assigned a new position

        :return: point cloud referenced to global world coordinates
        """
        if self.global_point_cloud_cache is None:
            world_point_cloud = copy.deepcopy(self.get_world_point_cloud())
            self.global_point_cloud_cache = world_point_cloud.transform(self.global_pose)

        return self.global_point_cloud_cache

    
    # TODO: move to visualizer.py
    def get_world_point_cloud(self) -> o3d.geometry.PointCloud:
        """
        The world referenced point cloud cached prior to pose transformation.
        This cache never needs to be updated

        :return: Point cloud which has been georeferenced with respect to
            global pose
        """
        if self.world_point_cloud_cache is None:
            image_rgb = o3d.geometry.Image(self.image)
            image_depth = o3d.geometry.Image(self._get_depth_image())
            if any(image_rgb.get_max_bound() != image_depth.get_max_bound()):
                raise ValueError("Image shape does not match camera parameters")

            image_rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(image_rgb, image_depth, depth_scale=1.0, depth_trunc=numpy.inf, convert_rgb_to_intensity=False)
            camera_parameters = o3d.camera.PinholeCameraIntrinsic(**self.settings.camera.dict())
            
            point_cloud = o3d.geometry.PointCloud.create_from_rgbd_image(image_rgbd, camera_parameters)
            point_cloud = point_cloud.uniform_down_sample(self.settings.visualizer.downsample)

            # for unexplainable reasons, open3d loads images upside down
            # this line transforms them back to right side up
            point_cloud.transform([[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0], [0, 0, 0, 1]])

            self.world_point_cloud_cache = point_cloud
            
        return self.world_point_cloud_cache

    
    def get_gps_translation(self, reference: "Frame") -> numpy.ndarray:
        enu = pymap3d.geodetic2enu(*self.gps_coords, *reference.gps_coords)
        return numpy.array(enu)


    def get_imu_rotation(self, reference: Optional["Frame"] = None) -> numpy.ndarray:
        reference_orientation = (
            reference.imu_orientation
            if reference is not None
            else numpy.zeros(3)
        )
        return orientation_to_rotation(self.imu_orientation - reference_orientation)

    
    def image_to_geodetic_point(
        self,
        x_position: float,
        y_position: float,
        origin_frame: "Frame"
    ) -> numpy.ndarray:
        world_point = self.image_to_world_point(x_position, y_position)
        global_point = (self.global_pose @ numpy.append(world_point, 1))[:3]
        lat_lon_alt = pymap3d.enu2geodetic(*global_point, *origin_frame.gps_coords)

        return numpy.array(lat_lon_alt)


    def image_to_global_point(self, x_position: float, y_position: float, origin_frame: "Frame") -> numpy.ndarray:
        """
        Requires imu and gps
        Translate, then rotate

        :param x_position: 
        :param y_position: 
        :param reference: 
        :return: 
        """
        world_point = self.image_to_world_point(x_position, y_position)
        translation = self.get_gps_translation(origin_frame)
        rotation = self.get_imu_rotation()  # global rotation

        return rotation @ (world_point + translation)


    def get_global_footprint(self, origin_frame: "Frame") -> List[numpy.ndarray]:
        """
        TODO: cache
        """
        corners = [
            (0, 0),
            (self.settings.camera.width, 0),
            (self.settings.camera.width, self.settings.camera.height),
            (0, self.settings.camera.height)
        ]

        return [
            self.image_to_global_point(*corner, origin_frame)
            for corner in corners
        ]


    def image_to_global_point(self, x_position: float, y_position: float, origin_frame: "Frame") -> numpy.ndarray:
        """
        TODO: speedup

        :param x_position: TODO
        :param y_position: TODO
        :param origin_frame: TODO
        :return: TODO
        """
        world_point = self.image_to_world_point(x_position, y_position)

        return self.world_to_global_point(world_point, origin_frame)


    def world_to_global_point(self, world_point: numpy.ndarray, origin_frame: "Frame") -> numpy.ndarray:
        if self.gps_imu_extrinsic_cache is None:
            self.gps_imu_extrinsic_cache = get_pose(
                self.get_imu_rotation(),  # rotation is relative to north
                self.get_gps_translation(origin_frame)  # translation is relative to origin frame
            )

        return (self.gps_imu_extrinsic_cache @ numpy.append(world_point, 1))[:3]


    def _get_pixel_depth(self, x_position: float, y_position: float) -> float:
        """
        Depth is defined as the gps altitude for all pixels. Future work could
        integrate depth maps or project depth onto a flat surface prior using
        attitude information

        :param x_position: x position of the pixel
        :param y_position: y position of the pixel
        :return: depth at specified pixel value
        """
        return self.gps_coords[2]


    def _get_depth_image(self) -> numpy.ndarray:
        """
        Construct an image of pixel depth at every pixel position
        Used for constructing a depth image for open3d point cloud

        :return: depth image
        """
        image_shape = (self.settings.camera.height, self.settings.camera.width)

        depth = self._get_pixel_depth(0, 0)  # TODO
        depth_image = numpy.full(image_shape, depth, dtype=numpy.float32)
        return depth_image


    def __repr__(self) -> str:
        return f"Frame ({self.frame_num}, {self.key_frame_num}, {self.image_path})"