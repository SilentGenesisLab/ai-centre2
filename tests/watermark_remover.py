import os
import subprocess
import random
import json
import time
from pathlib import Path
import argparse

class VideoWatermarkRemover:
    def __init__(self, input_path, output_dir="output", keep_intermediates=False):
        self.input_path = input_path
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.temp_dir = self.output_dir / "temp"
        self.temp_dir.mkdir(exist_ok=True)
        self.keep_intermediates = keep_intermediates
        self._intermediate_files = set()
        self.cleanup_expired_intermediates()

    def _intermediate_path(self, filename):
        self.temp_dir.mkdir(exist_ok=True)
        path = self.temp_dir / filename
        self._intermediate_files.add(path)
        return path

    def cleanup_current_intermediates(self):
        """默认在本次处理结束后删除中间文件。"""
        if self.keep_intermediates:
            return
        for path in self._intermediate_files:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self._intermediate_files.clear()

    def cleanup_expired_intermediates(self):
        """删除超过 24 小时的中间文件。"""
        cutoff = time.time() - 24 * 60 * 60
        candidates = list(self.temp_dir.iterdir())
        candidates.extend(self.output_dir.glob("step*.mp4"))
        for path in candidates:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        
    def generate_white_noise(self, duration, sample_rate=44100):
        """生成白噪声音频文件"""
        noise_path = self._intermediate_path(
            f"white_noise_{Path(self.input_path).stem}.wav"
        )
        cmd = [
            "ffmpeg", "-f", "lavfi", 
            "-i", f"anoisesrc=d={duration}:c=white:r={sample_rate}",
            "-y", str(noise_path)
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return noise_path
    
    def get_video_duration(self):
        """获取视频时长"""
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            self.input_path
        ]
        result = subprocess.run(cmd, check=True, capture_output=True)
        return float(result.stdout.strip())
    
    def apply_gaussian_noise(self, intensity=4):
        """应用高斯噪点（亮度噪声）"""
        output_path = self._intermediate_path(
            f"step1_gaussian_{Path(self.input_path).stem}.mp4"
        )
        cmd = [
            "ffmpeg", "-i", self.input_path,
            "-vf", f"noise=alls={intensity}:allf=t+u",
            "-c:a", "copy",
            "-y", str(output_path)
        ]
        subprocess.run(cmd, check=True)
        return output_path
    
    def apply_color_noise(self, input_path, intensity=6):
        """应用色度噪声"""
        output_path = self._intermediate_path(
            f"step2_color_{Path(self.input_path).stem}.mp4"
        )
        cmd = [
            "ffmpeg", "-i", str(input_path),
            "-vf", (
                f"noise=c1s={intensity}:c1f=t+u:"
                f"c2s={intensity}:c2f=t+u"
            ),
            "-c:a", "copy",
            "-y", str(output_path)
        ]
        subprocess.run(cmd, check=True)
        return output_path
    
    def apply_geometric_distortion(self, input_path):
        """应用几何扰动（缩放+旋转）"""
        output_path = self._intermediate_path(
            f"step3_geo_{Path(self.input_path).stem}.mp4"
        )
        # 随机旋转角度
        rotation = random.uniform(-0.003, 0.003)
        cmd = [
            "ffmpeg", "-i", str(input_path),
            "-vf", (
                "scale=trunc(iw*0.97/2)*2:trunc(ih*0.97/2)*2,"
                f"rotate={rotation}*random(0)"
            ),
            "-c:a", "copy",
            "-y", str(output_path)
        ]
        subprocess.run(cmd, check=True)
        return output_path
    
    def add_audio_noise(self, input_path, noise_level=-35):
        """添加音频白噪声"""
        duration = self.get_video_duration()
        noise_path = self.generate_white_noise(duration)
        
        output_path = self._intermediate_path(
            f"step4_audio_{Path(self.input_path).stem}.mp4"
        )
        cmd = [
            "ffmpeg", "-i", str(input_path),
            "-i", str(noise_path),
            "-filter_complex", 
            f"[1:a]volume={noise_level}dB[noise];[0:a][noise]amix=inputs=2",
            "-c:v", "copy",
            "-y", str(output_path)
        ]
        subprocess.run(cmd, check=True)
        return output_path
    
    def apply_frame_interpolation(self, input_path):
        """应用帧间随机延迟（每5帧插入1帧模糊过渡）"""
        output_path = self._intermediate_path(
            f"step5_frame_{Path(self.input_path).stem}.mp4"
        )
        cmd = [
            "ffmpeg", "-i", str(input_path),
            "-vf", "framerate=30:interp_start=0:interp_end=1:scene=100",
            "-y", str(output_path)
        ]
        subprocess.run(cmd, check=True)
        return output_path
    
    def transcode_hevc(self, input_path, bitrate_variation=10):
        """HEVC转码（二次编码）"""
        output_path = self._intermediate_path(
            f"step6_hevc_{Path(self.input_path).stem}.mp4"
        )
        
        # 随机调整码率
        base_bitrate = 2000  # 基础码率 kbps
        variation = random.uniform(-bitrate_variation, bitrate_variation)
        actual_bitrate = base_bitrate * (1 + variation / 100)
        
        cmd = [
            "ffmpeg", "-i", str(input_path),
            "-c:v", "libx265",
            "-b:v", f"{actual_bitrate}k",
            "-minrate", f"{actual_bitrate * 0.9}k",
            "-maxrate", f"{actual_bitrate * 1.1}k",
            "-bufsize", f"{actual_bitrate * 2}k",
            "-c:a", "aac",
            "-b:a", "128k",
            "-y", str(output_path)
        ]
        subprocess.run(cmd, check=True)
        return output_path
    
    def full_process_light(self):
        """轻度脱敏流程"""
        print("开始轻度脱敏处理...")
        try:
            # Step 1: 高斯噪点
            print("1. 应用高斯噪点...")
            result = self.apply_gaussian_noise(intensity=4)

            # Step 2: 音频白噪声
            print("2. 添加音频白噪声...")
            result = self.add_audio_noise(result, noise_level=-35)

            final_output = self.output_dir / f"final_light_{Path(self.input_path).stem}.mp4"
            result.replace(final_output)
            self._intermediate_files.discard(result)
            print(f"[OK] 轻度脱敏完成：{final_output}")
            return final_output
        finally:
            self.cleanup_current_intermediates()
    
    def full_process_intensive(self):
        """高强度脱敏流程"""
        print("开始高强度脱敏处理...")
        try:
            # Step 1: 高斯噪点
            print("1. 应用高斯噪点...")
            result = self.apply_gaussian_noise(intensity=5)

            # Step 2: 色度噪声
            print("2. 应用色度噪声...")
            result = self.apply_color_noise(result)

            # Step 3: 几何扰动
            print("3. 应用几何扰动...")
            result = self.apply_geometric_distortion(result)

            # Step 4: 音频白噪声
            print("4. 添加音频白噪声...")
            result = self.add_audio_noise(result, noise_level=-38)

            # Step 5: 帧间扰动
            print("5. 应用帧间扰动...")
            result = self.apply_frame_interpolation(result)

            # Step 6: HEVC转码
            print("6. HEVC二次转码...")
            result = self.transcode_hevc(result, bitrate_variation=10)

            # Step 7: 再次H.264转码（形成双重编码闭环）
            print("7. H.264二次转码...")
            final_output = self.transcode_h264(result)

            print(f"[OK] 高强度脱敏完成：{final_output}")
            return final_output
        finally:
            self.cleanup_current_intermediates()
    
    def transcode_h264(self, input_path):
        """H.264转码"""
        output_path = self.output_dir / f"final_h264_{Path(self.input_path).stem}.mp4"
        cmd = [
            "ffmpeg", "-i", str(input_path),
            "-c:v", "libx264",
            "-crf", "23",
            "-preset", "medium",
            "-c:a", "aac",
            "-b:a", "128k",
            "-y", str(output_path)
        ]
        subprocess.run(cmd, check=True)
        return output_path
    
    def batch_process(self, input_dir, mode="light"):
        """批量处理目录中的视频"""
        video_extensions = ['.mp4', '.avi', '.mov', '.mkv']
        results = []
        
        for file in Path(input_dir).iterdir():
            if file.suffix.lower() in video_extensions:
                print(f"\n处理文件: {file.name}")
                self.input_path = str(file)
                
                if mode == "light":
                    result = self.full_process_light()
                else:
                    result = self.full_process_intensive()
                
                results.append({
                    "original": file.name,
                    "processed": str(result),
                    "mode": mode
                })
        
        # 保存处理记录
        with open(self.output_dir / "processing_log.json", "w") as f:
            json.dump(results, f, indent=2)
        
        return results

def main():
    parser = argparse.ArgumentParser(description="视频暗水印脱敏处理工具")
    parser.add_argument("input", help="输入视频文件或目录")
    parser.add_argument("--mode", choices=["light", "intensive"], default="light",
                       help="处理模式: light=轻度脱敏, intensive=高强度脱敏")
    parser.add_argument("--output", default="output", help="输出目录")
    parser.add_argument("--batch", action="store_true", help="批量处理模式")
    parser.add_argument("--keep-intermediates", action="store_true",
                       help="保留中间文件；下次运行时清理超过24小时的文件（默认立即删除）")
    
    args = parser.parse_args()
    
    processor = VideoWatermarkRemover(
        args.input, args.output, keep_intermediates=args.keep_intermediates
    )
    
    if args.batch or os.path.isdir(args.input):
        results = processor.batch_process(args.input, args.mode)
        print(f"\n批量处理完成，共处理 {len(results)} 个文件")
        print(f"日志保存在: {processor.output_dir / 'processing_log.json'}")
    else:
        processor.input_path = args.input
        if args.mode == "light":
            result = processor.full_process_light()
        else:
            result = processor.full_process_intensive()
        print(f"\n处理完成！输出文件: {result}")

if __name__ == "__main__":
    main()
