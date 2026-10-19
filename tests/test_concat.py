"""Short synthetic media only: no GUI, gameplay, ASR or providers."""
import io
import json
from pathlib import Path
from unittest.mock import patch
import subprocess
import sys

import numpy as np
import pytest

from automation.cli import run_cli
from export.concat import ConcatError, concat_videos
from utils.ffmpeg import ensure_ffmpeg, run_cmd
from utils.subprocess_utils import run_no_window


@pytest.fixture(scope="module")
def tools():
    return ensure_ffmpeg()


@pytest.fixture(scope="module")
def clips(tmp_path_factory, tools):
    ffmpeg, _ = tools
    root = tmp_path_factory.mktemp("concat è 🎬")
    paths = {}
    for container, audio in [("mp4","aac"),("mkv","pcm_s16le")]:
        for index, color, frequency in [(1,"red",440),(2,"blue",880)]:
            file = root / f"clip {index} l'eroe.{container}"
            cmd = [ffmpeg,"-nostdin","-v","error","-f","lavfi","-i",
                   f"color=c={color}:s=160x90:r=30:d=1.5","-f","lavfi","-i",
                   f"sine=f={frequency}:r=48000:d=1.5","-c:v","libx264","-preset","ultrafast",
                   "-bf","2" if container=="mp4" else "0","-pix_fmt","yuv420p",
                   "-c:a",audio,"-shortest","-y",str(file)]
            result = run_no_window(cmd,capture_output=True)
            assert result.returncode == 0,result.stderr
            paths[container,index] = file
    return paths


def decode(file, tools):
    ffmpeg,_ = tools
    video = run_no_window([ffmpeg,"-v","error","-xerror","-i",str(file),"-map","0:v:0",
                           "-pix_fmt","rgb24","-fps_mode","passthrough","-f","rawvideo","pipe:1"],
                          capture_output=True,timeout=20)
    audio = run_no_window([ffmpeg,"-v","error","-xerror","-i",str(file),"-map","0:a:0",
                           "-ac","1","-ar","48000","-f","f32le","pipe:1"],
                          capture_output=True,timeout=20)
    assert video.returncode == audio.returncode == 0,(video.stderr,audio.stderr)
    return np.frombuffer(video.stdout,np.uint8).reshape(-1,90,160,3),np.frombuffer(audio.stdout,"<f4")


@pytest.mark.parametrize("container",["mp4","mkv"])
def test_real_join_order_duration_audio_and_junction(clips,tools,tmp_path,container):
    output = tmp_path / f"directory è 🎬/joined video.{container}"
    joined = concat_videos(clips[container,1],clips[container,2],output)
    assert joined.path == output.resolve()
    assert joined.duration_s == pytest.approx(3,abs=.002)
    frames,audio = decode(output,tools)
    assert len(frames) == 90
    # Check every decoded frame, including the exact A -> B splice.
    colors = frames.mean(axis=(1,2))
    assert np.all(colors[:45,0] > 240) and np.all(colors[:45,2] < 10)
    assert np.all(colors[45:,2] > 240) and np.all(colors[45:,0] < 10)
    for i in (1,2):
        source_frames,source_audio = decode(clips[container,i],tools)
        np.testing.assert_array_equal(frames[(i-1)*45:i*45],source_frames)
        if container == "mkv":
            np.testing.assert_array_equal(audio[(i-1)*72000:i*72000],source_audio)
    assert abs(len(audio)/48000-3) <= .022
    for start,expected in [(.25,440),(1.75,880)]:
        samples = audio[int(start*48000):int((start+.25)*48000)]
        frequencies = np.fft.rfftfreq(len(samples),1/48000)
        observed = frequencies[np.argmax(np.abs(np.fft.rfft(samples)))]
        assert observed == pytest.approx(expected,abs=4)
    for center in (1.47,1.49,1.51,1.53):
        samples = audio[int((center-.01)*48000):int((center+.01)*48000)]
        assert np.sqrt(np.mean(samples**2)) > .06  # no silence around splice
    assert joined.audio_reencoded == (container == "mp4")
    assert not list(output.parent.glob(".recut-concat-*"))


def test_cli_completion_and_json(clips,tmp_path):
    out,err = io.StringIO(),io.StringIO()
    file = tmp_path / "joined.mp4"
    code = run_cli(["concat",str(clips["mp4",1]),str(clips["mp4",2]),"-o",str(file),"--json"],
                   stdout=out,stderr=err)
    assert code == 0,err.getvalue()
    result = json.loads(out.getvalue())
    assert result["ok"] and result["video_mode"] == "copy" and Path(result["output"]) == file.resolve()
    assert "audio normalization" in err.getvalue()


def test_missing_file_and_existing_output_are_clear_and_preserved(clips,tmp_path):
    output = tmp_path/"existing.mp4";output.write_bytes(b"user data")
    with pytest.raises(FileExistsError):
        concat_videos(clips["mp4",1],clips["mp4",2],output)
    assert output.read_bytes() == b"user data"
    out,err=io.StringIO(),io.StringIO()
    assert run_cli(["concat",str(tmp_path/"missing.mp4"),str(clips["mp4",2]),"-o",str(output)],
                   stdout=out,stderr=err) == 60
    assert "Video not found" in err.getvalue()


def test_different_resolution_normalizes_before_join(clips,tools,tmp_path):
    ffmpeg,_=tools
    other=tmp_path/"different.mp4"
    result=run_no_window([ffmpeg,"-v","error","-i",str(clips["mp4",2]),"-vf","scale=320:180",
                          "-c:v","libx264","-preset","ultrafast","-bf","2","-c:a","copy","-y",str(other)],
                         capture_output=True)
    assert result.returncode == 0,result.stderr
    output=tmp_path/"joined.mp4"
    joined=concat_videos(clips["mp4",1],other,output)
    assert joined.video_reencoded and joined.audio_reencoded
    frames,audio=decode(output,tools)
    assert len(frames)==90 and abs(len(audio)/48000-3)<.022
    assert frames[44,:,:,0].mean()>240 and frames[45,:,:,2].mean()>240


def test_without_audio_video_copy_and_layout_mismatch(clips,tools,tmp_path):
    ffmpeg,_=tools
    silent=[]
    for i in (1,2):
        file=tmp_path/f"silent {i}.mp4"
        result=run_no_window([ffmpeg,"-v","error","-i",str(clips["mp4",i]),"-an","-c:v","copy","-y",str(file)],
                             capture_output=True)
        assert result.returncode == 0,result.stderr
        silent.append(file)
    output=tmp_path/"silent joined.mp4"
    assert not concat_videos(*silent,output).audio_reencoded
    with pytest.raises(ConcatError,match="stream layout"):
        concat_videos(silent[0],clips["mp4",2],tmp_path/"bad.mp4")


def test_ffmpeg_failure_cleans_partial(clips,tmp_path):
    output=tmp_path/"bad.mp4"
    with patch("export.concat.run_no_window",return_value=subprocess.CompletedProcess([],1,"","simulated failure")):
        with pytest.raises(ConcatError,match="simulated failure"):
            concat_videos(clips["mp4",1],clips["mp4",2],output)
    assert not output.exists() and not list(tmp_path.glob(".recut-concat-*"))


def test_publication_race_never_overwrites(clips,tmp_path):
    output=tmp_path/"race.mkv"
    from export.output_safety import atomic_promote_output
    def raced(temporary,destination,*,overwrite):
        destination.write_bytes(b"other user's output")
        atomic_promote_output(temporary,destination,overwrite=overwrite)
    with patch("export.concat.atomic_promote_output",side_effect=raced),pytest.raises(FileExistsError):
        concat_videos(clips["mkv",1],clips["mkv",2],output)
    assert output.read_bytes() == b"other user's output"


def test_help_and_import_are_headless():
    code = "from automation.cli import run_cli; import sys; run_cli(['concat','--help']); " \
           "assert not any(k.startswith(('PySide6.QtWidgets','analysis.gameplay','analysis.boundaries')) for k in sys.modules)"
    result=run_no_window([sys.executable,"-c",code],capture_output=True,text=True,timeout=15)
    assert result.returncode == 0,result.stderr
    assert "First local video" in result.stdout

def test_real_cli_from_other_cwd_never_imports_analysis_or_gui(clips,tmp_path):
    repository = Path(__file__).resolve().parents[1]
    args = [str(clips["mkv",1]), str(clips["mkv",2]), "-o", str(tmp_path/"cli output.mkv")]
    script = ("import sys; sys.path.insert(0,sys.argv.pop(1)); "
              "from automation.cli import run_cli; "
              "code=run_cli(['concat',*sys.argv[1:]]); "
              "assert code==0; "
              "assert 'automation.runtime' not in sys.modules; "
              "assert not any(k.startswith(('PySide6.QtWidgets','analysis.gameplay','analysis.boundaries')) for k in sys.modules)")
    result=run_no_window([sys.executable,"-c",script,str(repository),*args],
                         cwd=tmp_path,capture_output=True,text=True,encoding="utf-8",timeout=20)
    assert result.returncode == 0,result.stderr
    assert "DONE" in result.stdout and "cli output.mkv" in result.stdout


@pytest.fixture(scope="module")
def offset_clips(tmp_path_factory,tools):
    ffmpeg,_ = tools
    root = tmp_path_factory.mktemp("concat offsets")
    files = []
    for index in (0,1):
        file = root/f"part{index}.mp4"
        sound = ("sine=f=440:r=48000:d=1" if index==0 else
                 "aevalsrc=if(between(t\\,0.25\\,0.5)\\,0.3*sin(2*PI*880*t)\\,0):s=48000:d=1")
        command = [ffmpeg,"-v","error","-f","lavfi","-i","color=c=red:s=160x90:r=60:d=1",
                   "-f","lavfi","-i",sound,"-copyts"]
        if index:
            command += ["-vf","drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='gte(t,0.25)',setpts=PTS+2/(60*TB)"]
        else:
            command += ["-vf","setsar=1","-color_range","tv","-colorspace","bt709",
                        "-color_trc","bt709","-color_primaries","bt709"]
        command += ["-c:v","libx264","-preset","fast","-pix_fmt","yuv420p","-c:a","aac",
                    "-video_track_timescale","60" if index==0 else "90000","-y",str(file)]
        result=run_no_window(command,capture_output=True)
        assert result.returncode == 0,result.stderr
        files.append(file)
    return files


def test_timebase_codec_metadata_and_av_offset_are_normalized(offset_clips,tools,tmp_path):
    _,ffprobe=tools
    from export.concat import _probe,_plan
    source=[_probe(ffprobe,file) for file in offset_clips]
    plan=_plan(*source)
    assert plan.video_reencoded and "video.time_base" in plan.reasons
    assert "stream_offsets" in plan.reasons
    output=tmp_path/"normalized.mp4"
    notices=[]
    result=concat_videos(*offset_clips,output,on_status=notices.append)
    assert result.video_reencoded and result.audio_reencoded
    assert any("re-encoding video" in line for line in notices)
    frames,audio=decode(output,tools)
    assert len(frames)==122  # 1s A + 1s B with its original 2-frame audio lead
    assert len(audio)/48000 == pytest.approx(122/60,abs=.022)
    colors=frames.mean(axis=(1,2))
    first_blue=int(np.flatnonzero(colors[:,2]>200)[0])
    assert first_blue == 77  # A 60 + B initial 2 + B original 15 red frames
    # Audio B stays on its source clock: tone begins at 1.25s,
    # 33ms before the visual change. Do not silently shift the audio by 2 frames.
    pre=audio[int(1.20*48000):int(1.23*48000)]
    post=audio[int(1.26*48000):int(1.28*48000)]
    assert np.sqrt(np.mean(pre**2)) < .01
    assert np.sqrt(np.mean(post**2)) > .1
    # There must be no inserted black frames at the splice or initial hold.
    assert np.all(np.max(colors,axis=1)>200)


def test_unknown_color_sar_and_tiny_fps_rounding_do_not_force_reencode(clips,tools):
    from copy import deepcopy
    from export.concat import _probe,_plan
    _,ffprobe=tools
    first=_probe(ffprobe,clips["mp4",1])
    second=deepcopy(first)
    video=next(s for s in second if s["codec_type"]=="video")
    video["sample_aspect_ratio"]=None
    video["avg_frame_rate"]="900000000/30000001"
    for key in ("color_space","color_transfer","color_primaries","color_range"):
        video.pop(key,None)
    plan=_plan(first,second)
    assert not plan.video_reencoded and not plan.audio_reencoded


def test_audio_extradata_difference_normalizes_audio_only(clips,tools,tmp_path):
    from copy import deepcopy
    from export.concat import _probe as probe
    _,ffprobe=tools
    original=probe(ffprobe,clips["mp4",1])
    different=deepcopy(original)
    next(s for s in different if s["codec_type"]=="audio")["extradata_hash"]="different AAC config"
    output=tmp_path/"audio.mp4"
    def patched_probe(ffprobe,file):
        return different if file==clips["mp4",2] else probe(ffprobe,file)
    with patch("export.concat._probe",side_effect=patched_probe):
        result=concat_videos(clips["mp4",1],clips["mp4",2],output)
    assert result.audio_reencoded and not result.video_reencoded
    assert len(decode(output,tools)[0])==90


def test_normalization_failure_keeps_output_unpublished(offset_clips,tmp_path):
    output=tmp_path/"fail.mp4"
    with patch("export.concat.run_no_window",return_value=subprocess.CompletedProcess([],1,"","failed normalization")):
        with pytest.raises(ConcatError,match="failed normalization"):
            concat_videos(*offset_clips,output)
    assert not output.exists()
    assert not list(tmp_path.glob(".recut-concat-*"))

def test_hdr_or_explicit_color_conflicts_are_not_silently_retagged(clips,tools):
    from copy import deepcopy
    from export.concat import _probe,_plan
    _,ffprobe=tools
    first=_probe(ffprobe,clips["mp4",1])
    second=deepcopy(first)
    video=next(s for s in second if s["codec_type"]=="video")
    video["extradata_hash"]="changed"
    video["color_transfer"]="smpte2084"
    with pytest.raises(ConcatError,match="HDR normalization"):
        _plan(first,second)
    video["color_transfer"]="bt709"
    video["color_primaries"]="bt2020"
    reference=next(s for s in first if s["codec_type"]=="video")
    reference["color_primaries"]="bt709"
    with pytest.raises(ConcatError,match="explicit color conversion"):
        _plan(first,second)


def test_timecode_track_is_ignored_and_av_are_joined(clips,tools,tmp_path):
    ffmpeg,ffprobe=tools
    intro=tmp_path/"intro with timecode.mp4"
    generated=run_no_window([ffmpeg,"-v","error","-i",str(clips["mp4",1]),"-c","copy",
                             "-timecode","01:00:00:00","-y",str(intro)],capture_output=True)
    assert generated.returncode == 0,generated.stderr
    raw=json.loads(run_cmd([ffprobe,"-v","error","-show_streams","-of","json",str(intro)]).stdout)
    assert any(s.get("codec_tag_string")=="tmcd" for s in raw["streams"])
    output=tmp_path/"joined.mp4"
    result=concat_videos(intro,clips["mp4",2],output)
    frames,audio=decode(output,tools)
    assert len(frames)==90 and abs(len(audio)/48000-3)<.022
    assert frames[44,:,:,0].mean()>240 and frames[45,:,:,2].mean()>240
    checked=json.loads(run_cmd([ffprobe,"-v","error","-show_streams","-of","json",str(output)]).stdout)
    assert [s["codec_type"] for s in checked["streams"]]==["video","audio"]


def test_ancillary_indices_require_safe_normalization(clips,tools):
    from copy import deepcopy
    from export.concat import _probe,_plan
    _,ffprobe=tools
    first=_probe(ffprobe,clips["mp4",1])
    second=deepcopy(first)
    for stream in second:
        stream["index"]+=1  # auxiliary stream preceding the actual A/V
    assert _plan(first,second).video_reencoded


def test_extra_audio_is_not_silently_discarded(clips,tools):
    from export.concat import _probe
    _,ffprobe=tools
    raw=json.loads(run_cmd([ffprobe,"-v","error","-show_streams","-of","json",str(clips["mp4",1])]).stdout)
    raw["streams"].append(dict(raw["streams"][1],index=2))
    with patch("export.concat.run_cmd",return_value=subprocess.CompletedProcess([],0,json.dumps(raw),"")):
        with pytest.raises(ConcatError,match="Only one video"):
            _probe(ffprobe,clips["mp4",1])
