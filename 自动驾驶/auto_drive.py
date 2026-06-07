"""
自动驾驶系统 - 核心算法模块
包含车道检测、障碍物检测、车道偏离预警等功能
"""
import cv2
import numpy as np
import os


class AutoDriveSystem:
    """自动驾驶系统主类"""
    def __init__(self):
        self.lane_detector = LaneDetector()
        self.obstacle_detector = ObstacleDetector()
        self.speed_detector = SpeedDetector()
        self.heatmap_generator = HeatmapGenerator()
    
    def process_frame(self, frame):
        """处理单帧图像，完成所有检测"""
        # 车道线检测（包含LDW）
        lane_result, lane_info = self.lane_detector.process_image(frame.copy())
        
        # 障碍物检测
        obstacle_result, obstacles = self.obstacle_detector.detect(frame.copy())
        
        # 测速
        speed_info = self.speed_detector.calculate_speed(frame.copy())
        
        # 热力图
        heatmap = self.heatmap_generator.generate_heatmap(frame.copy())
        
        # 合成结果
        combined = cv2.addWeighted(lane_result, 0.6, obstacle_result, 0.4, 0)
        
        # 在画面上显示速度信息
        cv2.putText(combined, f"Speed: {speed_info['speed']:.1f} km/h", 
                   (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 0), 2)
        
        # 添加异常状态提示
        if lane_info.get('status') != 'normal':
            status = lane_info.get('status', 'normal')
            status_text = {
                'no_lane': 'No Lane Detected',
                'blurry': 'Lane Blurry',
                'night': 'Night Mode',
                'bright': 'Overexposed',
                'rain': 'Rain/Weather'
            }.get(status, status)
            
            cv2.putText(combined, f"Status: {status_text}", (10, 100), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 2)
        
        # LDW 告警
        if lane_info.get('ldw_warning'):
            cv2.putText(combined, "⚠ LANE DEPARTURE WARNING", (int(combined.shape[1]/2) - 200, 150),
                       cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
        
        return combined, {
            'obstacles': obstacles,
            'speed': speed_info,
            'heatmap': heatmap,
            'lane_info': lane_info
        }
    
    def process_image(self, image_path):
        """处理单张图片"""
        img_array = np.fromfile(image_path, dtype=np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        
        if img is None:
            return None, {}
        
        result, info = self.process_frame(img)
        return result, info
    
    def process_video(self, input_path, output_path):
        """处理视频文件"""
        cap = cv2.VideoCapture(input_path)
        
        if not cap.isOpened():
            return False
        
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            fps = 30
        fps = int(fps)
        
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
        if not output_path.lower().endswith('.mp4'):
            base_path = output_path.rsplit('.', 1)[0] if '.' in output_path else output_path
            output_path = base_path + '.mp4'
        
        fourcc_options = [
            ('avc1', 'H.264'),
            ('mp4v', 'MPEG-4'),
            ('XVID', 'XviD'),
        ]
        
        out = None
        for codec, _ in fourcc_options:
            fourcc = cv2.VideoWriter_fourcc(*codec)
            temp_out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
            if temp_out.isOpened():
                out = temp_out
                break
        
        if out is None or not out.isOpened():
            return False
        
        while cap.isOpened():
            ret, frame = cap.read()
            if ret:
                result, _ = self.process_frame(frame)
                out.write(result)
            else:
                break
        
        cap.release()
        out.release()
        return True


class LaneDetector:
    """增强的车道线检测器 - 包含LDW功能"""
    def __init__(self):
        self.prev_left_x = None
        self.prev_right_x = None
        self.ldw_threshold = 50  # 偏离阈值（像素）
        
    def detect_lane_status(self, img):
        """检测环境状态（夜间、模糊等）"""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        brightness = np.mean(gray)
        
        # 检测模糊度
        laplacian = cv2.Laplacian(gray, cv2.CV_64F)
        blur_score = np.var(laplacian)
        
        status = 'normal'
        
        # 夜间检测
        if brightness < 50:
            status = 'night'
        # 过亮检测
        elif brightness > 200:
            status = 'bright'
        # 模糊检测
        elif blur_score < 100:
            status = 'blurry'
        
        return status, brightness, blur_score
    
    def detect_lane_departure(self, left_lane_x, right_lane_x, img_width):
        """检测车道偏离"""
        if left_lane_x is None or right_lane_x is None:
            return False
        
        # 计算车道中心
        lane_center = (left_lane_x + right_lane_x) / 2
        # 图像中心
        img_center = img_width / 2
        # 计算偏离距离
        deviation = abs(lane_center - img_center)
        
        return deviation > self.ldw_threshold
    
    def draw_lane_lines(self, img, left_line, right_line, color=(0, 255, 0), thickness=5):
        """绘制车道线"""
        if left_line is not None:
            x1, y1, x2, y2 = left_line
            cv2.line(img, (x1, y1), (x2, y2), color, thickness)
        
        if right_line is not None:
            x1, y1, x2, y2 = right_line
            cv2.line(img, (x1, y1), (x2, y2), color, thickness)
    
    def draw_lane_area(self, img, left_line, right_line):
        """绘制车道区域"""
        if left_line is None or right_line is None:
            return
        
        height = img.shape[0]
        
        # 创建点
        left_pts = np.array([
            [left_line[0], height],
            [left_line[2], int(height * 0.4)],
            [right_line[2], int(height * 0.4)],
            [right_line[0], height]
        ], np.int32)
        
        # 绘制填充区域
        overlay = img.copy()
        cv2.fillPoly(overlay, [left_pts], (0, 255, 0, 50))
        cv2.addWeighted(overlay, 0.3, img, 0.7, 0, img)
        
        # 绘制边界
        cv2.polylines(img, [left_pts], False, (0, 255, 0), 2)
    
    def process_image(self, image):
        """处理图片并检测车道线"""
        # 检测环境状态
        status, brightness, blur_score = self.detect_lane_status(image)
        
        # 灰度化和模糊
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        
        # 边缘检测
        edges = cv2.Canny(blur, 50, 150)
        
        # 定义ROI区域
        height, width = image.shape[:2]
        vertices = np.array([[
            (100, height),
            (width//2 - 50, height//2 + 50),
            (width//2 + 50, height//2 + 50),
            (width - 100, height)
        ]], dtype=np.int32)
        
        # 遮罩
        mask = np.zeros_like(edges)
        cv2.fillPoly(mask, vertices, 255)
        masked_edges = cv2.bitwise_and(edges, mask)
        
        # 霍夫变换检测线段
        lines = cv2.HoughLinesP(masked_edges, 1, np.pi/180, 
                                threshold=30, minLineLength=30, maxLineGap=100)
        
        # 分别存储左右车道线
        left_lines = []
        right_lines = []
        
        if lines is not None:
            for line in lines:
                x1, y1, x2, y2 = line[0]
                # 计算斜率
                if x2 != x1:
                    slope = (y2 - y1) / (x2 - x1)
                    
                    # 根据斜率分类左右车道
                    if slope < -0.5:  # 左车道（向右下）
                        left_lines.append((x1, y1, x2, y2))
                    elif slope > 0.5:  # 右车道（向左下）
                        right_lines.append((x1, y1, x2, y2))
        
        # 绘制结果
        line_img = np.zeros_like(image)
        
        left_lane = None
        right_lane = None
        
        # 拟合左车道线
        if len(left_lines) > 0:
            left_lane = self.fit_lane(left_lines, height)
            if left_lane is not None:
                cv2.line(line_img, (left_lane[0], left_lane[1]), 
                        (left_lane[2], left_lane[3]), (0, 0, 255), 5)  # 红色
        
        # 拟合右车道线
        if len(right_lines) > 0:
            right_lane = self.fit_lane(right_lines, height)
            if right_lane is not None:
                cv2.line(line_img, (right_lane[0], right_lane[1]),
                        (right_lane[2], right_lane[3]), (0, 255, 0), 5)  # 绿色
        
        # 绘制车道区域
        if left_lane is not None and right_lane is not None:
            self.draw_lane_area(line_img, left_lane, right_lane)
        
        # 检测车道偏离
        ldw_warning = False
        left_x = left_lane[0] if left_lane else None
        right_x = right_lane[0] if right_lane else None
        
        if left_lane is not None and right_lane is not None:
            ldw_warning = self.detect_lane_departure(left_x, right_x, width)
            
            # 如果偏离，绘制警告框
            if ldw_warning:
                cv2.rectangle(line_img, (50, 50), (width-50, height-50), 
                            (0, 0, 255), 5)
        
        # 合并结果
        result = cv2.addWeighted(image, 0.8, line_img, 1.0, 0)
        
        # 构建车道信息
        lane_info = {
            'status': status,
            'brightness': float(brightness),
            'blur_score': float(blur_score),
            'left_lane': left_lane,
            'right_lane': right_lane,
            'ldw_warning': ldw_warning,
            'lanes_detected': left_lane is not None or right_lane is not None
        }
        
        return result, lane_info
    
    def fit_lane(self, lines, height):
        """拟合车道线"""
        if len(lines) == 0:
            return None
        
        x_coords = []
        y_coords = []
        
        for x1, y1, x2, y2 in lines:
            x_coords.extend([x1, x2])
            y_coords.extend([y1, y2])
        
        if len(x_coords) < 2:
            return None
        
        # 线性拟合
        try:
            poly = np.polyfit(y_coords, x_coords, 1)
            
            # 从底部到顶部画线
            y1 = height
            y2 = int(height * 0.4)
            x1 = int(poly[0] * y1 + poly[1])
            x2 = int(poly[0] * y2 + poly[1])
            
            return (x1, y1, x2, y2)
        except:
            return None


class ObstacleDetector:
    """障碍物检测器"""
    def __init__(self):
        self.fgbg = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=40, detectShadows=True)
    
    def detect(self, img):
        """检测障碍物"""
        result = img.copy()
        obstacles = []
        
        # 前景提取
        fgmask = self.fgbg.apply(img)
        
        # 形态学处理
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_OPEN, kernel)
        fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_CLOSE, kernel)
        
        # 边缘检测
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        
        # 结合前景和边缘
        combined = cv2.bitwise_and(fgmask, edges)
        
        # 查找轮廓
        contours, _ = cv2.findContours(combined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        for contour in contours:
            area = cv2.contourArea(contour)
            if area > 500:  # 过滤小轮廓
                x, y, w, h = cv2.boundingRect(contour)
                
                # 只检测道路区域的障碍物
                if y > img.shape[0] * 0.4:
                    cv2.rectangle(result, (x, y), (x + w, y + h), (0, 255, 0), 2)
                    cv2.putText(result, "Obstacle", (x, y - 10), 
                               cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                    
                    obstacles.append({
                        'x': int(x),
                        'y': int(y),
                        'width': int(w),
                        'height': int(h),
                        'area': float(area)
                    })
        
        # 显示检测信息
        cv2.putText(result, f"Obstacles: {len(obstacles)}", (10, 30), 
                   cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        
        return result, obstacles


class SpeedDetector:
    """速度检测器"""
    def __init__(self):
        self.prev_gray = None
        self.speed = 0.0
        self.frame_count = 0
        
    def calculate_speed(self, frame):
        """基于光流法计算速度"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (320, 240))
        
        speed_info = {
            'speed': 0.0,
            'flow_magnitude': 0.0,
            'direction': 'stationary'
        }
        
        if self.prev_gray is not None:
            # 计算光流
            flow = cv2.calcOpticalFlowFarneback(
                self.prev_gray, gray,
                None,
                0.5, 3, 15, 3, 5, 1.2, 0
            )
            
            # 计算平均流速
            magnitude, angle = cv2.cartToPolar(flow[..., 0], flow[..., 1])
            avg_magnitude = float(np.mean(magnitude))
            
            # 转换为速度
            self.speed = float(avg_magnitude) * 5.0
            self.speed = min(self.speed, 120.0)
            
            speed_info['speed'] = self.speed
            speed_info['flow_magnitude'] = avg_magnitude
            
            # 判断方向
            if avg_magnitude < 0.5:
                speed_info['direction'] = 'stationary'
            elif avg_magnitude < 2.0:
                speed_info['direction'] = 'slow'
            elif avg_magnitude < 5.0:
                speed_info['direction'] = 'moderate'
            else:
                speed_info['direction'] = 'fast'
        
        self.prev_gray = gray.copy()
        self.frame_count += 1
        
        return speed_info
    
    def reset(self):
        """重置"""
        self.prev_gray = None
        self.speed = 0.0
        self.frame_count = 0


class HeatmapGenerator:
    """热力图生成器"""
    def __init__(self):
        self.accumulated_heatmap = None
        self.frame_count = 0
        self.max_frames = 30
        
    def generate_heatmap(self, frame):
        """生成热力图"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (320, 240))
        
        # 计算梯度
        gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        gradient_magnitude = np.sqrt(gx**2 + gy**2)
        
        # 归一化
        gradient_magnitude = cv2.normalize(gradient_magnitude, None, 0, 255, cv2.NORM_MINMAX)
        gradient_magnitude = gradient_magnitude.astype(np.uint8)
        
        # 初始化或累积
        if self.accumulated_heatmap is None:
            self.accumulated_heatmap = np.zeros_like(gradient_magnitude)
        
        self.accumulated_heatmap = cv2.addWeighted(
            self.accumulated_heatmap, 0.9,
            gradient_magnitude, 0.1, 0
        )
        
        self.frame_count += 1
        
        if self.frame_count > self.max_frames:
            self.accumulated_heatmap = cv2.addWeighted(
                self.accumulated_heatmap, 0.95,
                gradient_magnitude, 0.05, 0
            )
        
        # 颜色映射
        heatmap_color = cv2.applyColorMap(self.accumulated_heatmap, cv2.COLORMAP_JET)
        heatmap_resized = cv2.resize(heatmap_color, (frame.shape[1], frame.shape[0]))
        
        return heatmap_resized
    
    def reset(self):
        """重置"""
        self.accumulated_heatmap = None
        self.frame_count = 0
