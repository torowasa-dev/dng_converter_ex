import contextlib
import io
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageCms, ImageOps
from PIL.TiffImagePlugin import IFDRational

from raw_to_dng.cli import main
from raw_to_dng.core import Converter, Mode, Settings, Source, plan_jobs, scan_inputs
from raw_to_dng.dng import inspect_dng
from raw_to_dng.jpeg_dng import write_jpeg_dng
from tests.fixtures import make_dng


class JpegDngTests(unittest.TestCase):
    def test_forced_conversion_uses_source_thumbnail_when_initial_wb_is_blank(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, bright = root / "photo.jpg", root / "bright.jpg"
            Image.new("RGB", (160, 120), (70, 85, 110)).save(source)
            Image.new("RGB", (80, 60), (140, 155, 180)).save(bright)
            with Image.open(source) as original, Image.open(bright) as rendered:
                expected, adobe = original.getpixel((0, 0)), rendered.getpixel((0, 0))

            def transcode(command, **_kwargs):
                stage = Path(command[command.index("-d") + 1]) / command[command.index("-o") + 1]
                write_jpeg_dng(bright, stage, lambda: None)
                return SimpleNamespace(pid=123, returncode=0, poll=lambda: 0)

            for kelvin, pixel in ((None, expected), (5200, adobe)):
                settings = Settings(mode=Mode.UNCOMPRESSED, force_jpeg_to_dng=True, jpeg_wb_kelvin=kelvin)
                job, = plan_jobs([Source(source)], root / str(kelvin), settings)
                with (patch("raw_to_dng.core.resolve_converter", return_value=root / "Adobe.exe"),
                      patch("raw_to_dng.core.subprocess.Popen", side_effect=transcode)):
                    result = Converter(root / "Adobe.exe").convert(job)
                self.assertEqual(result.status, "ok", result.message)
                self.assertEqual(result.output_bytes, job.destination.stat().st_size)
                with Image.open(job.destination) as preview:
                    self.assertEqual(preview.size, (80, 60))
                    self.assertEqual(preview.getpixel((0, 0)), pixel)

    def test_force_jpeg_cli_applies_final_options_and_retries_oversize_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "test.JPG"
            exif = Image.Exif()
            exif[34665] = {36867: "2026:11:08 10:20:30", 34855: 400}
            Image.new("RGB", (160, 120), (140, 120, 80)).save(source, exif=exif)
            original = source.read_bytes()
            output = root / "out"
            intermediate_paths = []
            distances = []

            def transcode(command, **_kwargs):
                intermediate = Path(command[-1])
                self.assertEqual(intermediate.suffix, ".dng")
                info = inspect_dng(intermediate)
                self.assertEqual((info.width, info.height, info.compression), (160, 120, 1))
                with Image.open(intermediate) as preview:
                    self.assertEqual(preview.getexif().get_ifd(34665)[34855], 400)
                    self.assertNotEqual(preview.tag_v2[50728], (1, 1, 1))
                intermediate_paths.append(intermediate)
                distances.append(command[command.index("-jxl_distance") + 1])
                self.assertEqual(command[command.index("-jxl_effort") + 1], "8")
                self.assertEqual(command[command.index("-side") + 1], "64")
                self.assertIn("-p0", command)
                self.assertIn("-fl", command)
                stage = Path(command[command.index("-d") + 1]) / command[command.index("-o") + 1]
                make_dng(stage, width=64, height=48, linear=True, compression=52546)
                if len(distances) == 1:
                    with stage.open("r+b") as stream:
                        stream.truncate(2000)
                return SimpleNamespace(pid=123, returncode=0, poll=lambda: 0)

            fake = root / "Adobe.exe"
            with (patch("raw_to_dng.cli.resolve_converter", return_value=fake),
                  patch("raw_to_dng.core.resolve_converter", return_value=fake),
                  patch("raw_to_dng.core.converter_version", return_value="17.0"),
                  patch("raw_to_dng.core.subprocess.Popen", side_effect=transcode),
                  contextlib.redirect_stdout(io.StringIO())):
                status = main(["convert", str(root), "-o", str(output), "--force-jpeg-to-dng",
                               "--mode", "lossy-jxl", "--distance", "0.2", "--effort", "8",
                               "--long-edge", "64", "--preview", "0", "--wb-kelvin", "3200",
                               "--max-file-mb", "0.001", "--oversize-action", "increase-distance",
                               "--distance-step", "0.05", "--no-auto-retry"])
            self.assertEqual(status, 0)
            self.assertEqual(distances, ["0.2", "0.25"])
            self.assertTrue(all(not path.exists() for path in intermediate_paths))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual([path.name for path in output.glob("*.dng")], ["test.dng"])
            self.assertEqual(inspect_dng(output / "test.dng").compression, 52546)

    def test_cli_folder_conversion_preserves_original_and_applies_exif_orientation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "photo.JPG"
            exif = Image.Exif()
            exif.endian = ">"
            exif[274] = 6
            exif[271] = "Camera maker"
            exif[34665] = {
                33434: IFDRational(1, 250), 33437: IFDRational(28, 10), 34855: 400,
                36867: "2026:11:07 12:34:56", 37380: IFDRational(-2, 3),
                37386: IFDRational(35, 1), 37500: b"Example opaque MakerNote", 42036: "35mm lens",
            }
            exif[34853] = {1: "N", 2: (IFDRational(34), IFDRational(30), IFDRational(0))}
            image = Image.new("RGB", (48, 32), (128, 80, 220))
            icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            image.save(source, exif=exif, icc_profile=icc)
            original_bytes = source.read_bytes()
            (root / "ignore.nef").touch()
            out = root / "output"
            with contextlib.redirect_stdout(io.StringIO()):
                status = main(["convert", str(root), "-o", str(out), "--mode", "jpeg-to-dng",
                               "--wb-kelvin", "3200"])
            self.assertEqual(status, 0)
            self.assertEqual(source.read_bytes(), original_bytes)
            result = out / "photo.dng"
            info = inspect_dng(result)
            self.assertEqual((info.width, info.height, info.bits_per_sample), (32, 48, (16, 16, 16)))
            self.assertEqual(info.raw_kind, "Linear RAW")
            self.assertTrue(info.wb_metadata_ready)
            with Image.open(source) as jpeg:
                source_exif = jpeg.info["exif"]
                source_capture = jpeg.getexif().get_ifd(34665)
            with Image.open(result) as preview:
                target_exif = preview.getexif()
                self.assertNotIn(50879, preview.tag_v2)
                target_capture = target_exif.get_ifd(34665)
                for tag in (33434, 33437, 34855, 36867, 37380, 37386, 42036):
                    self.assertEqual(target_capture[tag], source_capture[tag])
                self.assertEqual(target_exif[271], "Camera maker")
                self.assertEqual(tuple(target_exif.get_ifd(34853)[2]), (34, 30, 0))
                self.assertEqual((target_capture[40962], target_capture[40963]), (32, 48))
                self.assertEqual(bytes(preview.tag_v2[50740]), b"DngConverterEx\0" + source_exif)
                neutral = preview.tag_v2[50728]
                self.assertGreater(neutral[0], neutral[1])
                self.assertGreater(neutral[1], neutral[2])
                raw_offset = preview.tag_v2[330][0]
            with result.open("rb") as stream:
                stream.seek(raw_offset)
                count = struct.unpack("<H", stream.read(2))[0]
                tags = dict((tag, value) for tag, _, _, value in struct.iter_unpack("<HHII", stream.read(12 * count)))
                stream.seek(tags[273])
                first_pixel = struct.unpack("<3H", stream.read(6))
            with Image.open(source) as jpeg:
                red, green, blue = ImageOps.exif_transpose(jpeg).getpixel((0, 0))
            expected = tuple(round((((value / 255 + 0.055) / 1.055) ** 2.4) * 65535)
                             for value in (red, green, blue))
            self.assertEqual(first_pixel, expected)

    def test_wb_kelvin_validation_and_default_neutral(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "photo.jpg"
            Image.new("RGB", (48, 32)).save(source)
            for invalid in (1999, 25001, 5600.5):
                with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, "WBは"):
                    Settings(mode=Mode.JPEG_TO_DNG, jpeg_wb_kelvin=invalid).validate()
            with self.assertRaisesRegex(ValueError, "JPEG強制変換"):
                Settings(jpeg_wb_kelvin=5600).validate()
            job, = plan_jobs([Source(source)], root / "output", Settings(mode=Mode.JPEG_TO_DNG))
            self.assertEqual(Converter(None).convert(job).status, "ok")
            with Image.open(job.destination) as preview:
                self.assertNotIn(50728, preview.tag_v2)
                self.assertEqual(tuple(map(float, preview.tag_v2[50729])), (0.34567, 0.35850))
                self.assertEqual(preview.tag_v2[50879], 1)
                self.assertEqual(preview.tag_v2[50730], 0)
                self.assertEqual(preview.tag_v2[50940], (0.0, 0.0, 1.0, 1.0))
                self.assertEqual(len(preview.tag_v2[50964]), 9)
                self.assertEqual(preview.tag_v2[51109], 0)
                self.assertEqual(preview.tag_v2[51110], 1)
                to_srgb, from_srgb = (tuple(map(float, preview.tag_v2[tag])) for tag in (50721, 50964))
            for row in range(3):
                for column in range(3):
                    product = sum(to_srgb[3 * row + k] * from_srgb[3 * k + column] for k in range(3))
                    self.assertAlmostEqual(product, float(row == column), places=6)
            info = inspect_dng(job.destination)
            self.assertTrue(info.wb_metadata_ready)
            self.assertEqual(info.colorimetric_reference, 1)
            self.assertEqual(info.backward_version, "1.2.0.0")

    def test_output_referred_jpeg_round_trips_all_tone_levels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, stage = root / "ramp.jpg", root / "ramp.dng"
            Image.frombytes("L", (256, 8), bytes(range(256)) * 8).save(source, quality=100)
            with Image.open(source) as original:
                expected = tuple(original.tobytes()[:256])
            write_jpeg_dng(source, stage, lambda: None)
            with Image.open(stage) as preview, stage.open("rb") as stream:
                stream.seek(preview.tag_v2[330][0])
                count = struct.unpack("<H", stream.read(2))[0]
                tags = {tag: value for tag, _, _, value in struct.iter_unpack("<HHII", stream.read(12 * count))}
                stream.seek(tags[273])
                linear = struct.unpack("<768H", stream.read(1536))[::3]
            self.assertEqual(len(set(expected)), 256)
            samples = (value / 65535 for value in linear)
            encoded = tuple(round(255 * (12.92 * value if value <= 0.0031308 else
                                         1.055 * value ** (1 / 2.4) - 0.055)) for value in samples)
            self.assertEqual(encoded, expected)

    def test_invalid_jpeg_and_size_limit_do_not_publish_or_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "photo.jpg"
            source.write_bytes(b"not a JPEG")
            output = root / "output"
            settings = Settings(mode=Mode.JPEG_TO_DNG, max_file_mb=0.001)
            job, = plan_jobs([Source(source)], output, settings)
            converter = Converter(None)
            self.assertEqual(converter.convert(job).status, "error")
            Image.new("RGB", (48, 32)).save(source)
            error = converter.convert(job)
            self.assertIn("最大ファイルサイズ超過", error.message)
            self.assertFalse(job.destination.exists())
            self.assertTrue(source.exists())
            with self.assertRaisesRegex(ValueError, "JPEGを変換するには"):
                scan_inputs([source])
