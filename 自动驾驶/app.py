"""
自动驾驶系统 - Flask Web应用
"""
from flask import Flask, render_template, request, jsonify, send_from_directory
import cv2
import numpy as np
import os
import mimetypes
import webbrowser
import threading
from werkzeug.utils import secure_filename
from auto_drive import AutoDriveSystem

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'static/output'
app.config['HEATMAP_FOLDER'] = 'static/heatmap'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)
os.makedirs(app.config['HEATMAP_FOLDER'], exist_ok=True)

auto_drive = AutoDriveSystem()

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'mp4', 'avi', 'mov'}

mimetypes.add_type('video/mp4', '.mp4')
mimetypes.add_type('video/x-msvideo', '.avi')
mimetypes.add_type('video/quicktime', '.mov')


def convert_numpy_types(obj):
    """递归转换 NumPy 类型为 Python 原生类型"""
    if isinstance(obj, dict):
        return {key: convert_numpy_types(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_numpy_types(item) for item in obj]
    elif isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    else:
        return obj


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file part'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No selected file'}), 400
        
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            input_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(input_path)
            
            base_name, file_ext = os.path.splitext(filename)
            file_ext = file_ext[1:].lower()
            
            # 根据文件类型设置输出文件名
            if file_ext in {'png', 'jpg', 'jpeg'}:
                output_filename = f'result_{base_name}.{file_ext}'
            else:
                output_filename = f'result_{base_name}.mp4'
            output_path = os.path.join(app.config['OUTPUT_FOLDER'], output_filename)
            
            heatmap_filename = f'heatmap_{filename}.jpg'
            heatmap_path = os.path.join(app.config['HEATMAP_FOLDER'], heatmap_filename)
            
            if file_ext in {'png', 'jpg', 'jpeg'}:
                result, info = auto_drive.process_image(input_path)
                if result is not None:
                    # 支持中文路径保存
                    ext = os.path.splitext(output_path)[1]
                    if ext in ['.jpg', '.jpeg']:
                        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 95]
                    else:  # .png
                        encode_param = [int(cv2.IMWRITE_PNG_COMPRESSION), 3]
                    
                    result_encode = cv2.imencode(ext, result, encode_param)[1]
                    result_encode.tofile(output_path)
                    
                    if 'heatmap' in info:
                        heatmap_encode = cv2.imencode('.jpg', info['heatmap'], [int(cv2.IMWRITE_JPEG_QUALITY), 95])[1]
                        heatmap_encode.tofile(heatmap_path)
                    
                    # 转换 NumPy 类型
                    obstacles = convert_numpy_types(info.get('obstacles', []))
                    speed = convert_numpy_types(info.get('speed', {}))
                    
                    return jsonify({
                        'success': True,
                        'type': 'image',
                        'input': filename,
                        'output': output_filename,
                        'heatmap': heatmap_filename,
                        'obstacles': obstacles,
                        'speed': speed
                    })
                else:
                    return jsonify({'success': False, 'error': 'Failed to process image'}), 500
            
            elif file_ext in {'mp4', 'avi', 'mov'}:
                success = auto_drive.process_video(input_path, output_path)
                if success:
                    return jsonify({
                        'success': True,
                        'type': 'video',
                        'input': filename,
                        'output': output_filename
                    })
                else:
                    return jsonify({'success': False, 'error': 'Failed to process video'}), 500
        
        return jsonify({'success': False, 'error': 'File type not allowed'}), 400
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/output/<filename>')
def send_output(filename):
    file_path = os.path.join(app.config['OUTPUT_FOLDER'], filename)
    
    if os.path.exists(file_path):
        # 确保使用正确的 MIME 类型
        if filename.lower().endswith('.mp4'):
            mime_type = 'video/mp4'
        elif filename.lower().endswith('.avi'):
            mime_type = 'video/x-msvideo'
        elif filename.lower().endswith('.mov'):
            mime_type = 'video/quicktime'
        elif filename.lower().endswith(('.jpg', '.jpeg', '.png')):
            ext = filename.rsplit(".", 1)[1].lower()
            mime_type = f'image/{ext}'
        else:
            mime_type, _ = mimetypes.guess_type(filename)
            if mime_type is None:
                mime_type = 'application/octet-stream'
        
        return send_from_directory(app.config['OUTPUT_FOLDER'], filename, mimetype=mime_type)
    else:
        return jsonify({'success': False, 'error': 'File not found', 'path': file_path}), 404


@app.route('/heatmap/<filename>')
def send_heatmap(filename):
    return send_from_directory(app.config['HEATMAP_FOLDER'], filename)


@app.route('/video/stream')
def video_stream():
    """实时视频流"""
    camera_id = request.args.get('camera', 0, type=int)
    
    def generate_frames():
        cap = cv2.VideoCapture(camera_id)
        try:
            while True:
                success, frame = cap.read()
                if not success:
                    break
                
                # 处理帧
                result, info = auto_drive.process_frame(frame)
                
                # 编码为 JPEG
                ret, buffer = cv2.imencode('.jpg', result)
                if not ret:
                    continue
                
                frame_data = buffer.tobytes()
                
                # 生成 MJPEG 流
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_data + b'\r\n')
        finally:
            cap.release()
    
    return app.response_class(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/camera/list')
def camera_list():
    """获取可用摄像头列表"""
    cameras = []
    
    # 测试前5个摄像头
    for i in range(5):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            ret, _ = cap.read()
            if ret:
                cameras.append({
                    'id': i,
                    'name': f'摄像头 {i+1}',
                    'status': 'available'
                })
            cap.release()
    
    return jsonify({
        'success': True,
        'cameras': cameras
    })


@app.errorhandler(404)
def not_found(e):
    return jsonify({'success': False, 'error': 'Page not found'}), 404


@app.errorhandler(500)
def internal_error(e):
    return jsonify({'success': False, 'error': 'Internal server error'}), 500


if __name__ == '__main__':
    def open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open('http://localhost:5000')
    
    browser_thread = threading.Thread(target=open_browser)
    browser_thread.daemon = True
    browser_thread.start()
    
    print("=" * 50)
    print("  启动自动驾驶系统")
    print("  访问地址: http://localhost:5000")
    print("=" * 50)
    
    app.run(debug=False, host='0.0.0.0', port=5000)
