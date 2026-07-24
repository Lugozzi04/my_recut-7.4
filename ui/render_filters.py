from analysis.cut_engine import Segment


def build_filter_complex(
    keeps: list[Segment],
    fps: float | None = None,
    force_audio_async: bool = True,
) -> str:
    parts = []
    for i, seg in enumerate(keeps):
        v_chain = f"[0:v]trim=start={seg.start}:end={seg.end},setpts=PTS-STARTPTS"
        if fps and fps > 1.0:
            v_chain += f",fps={fps:.3f}"
        parts.append(f"{v_chain}[v{i}];")
        parts.append(
            f"[0:a]atrim=start={seg.start}:end={seg.end},asetpts=PTS-STARTPTS[a{i}];"
        )
    concat_inputs = "".join([f"[v{i}][a{i}]" for i in range(len(keeps))])
    parts.append(f"{concat_inputs}concat=n={len(keeps)}:v=1:a=1[outv][outa0];")
    if force_audio_async:
        parts.append("[outa0]aresample=async=1:first_pts=0[outa]")
    else:
        parts.append("[outa0]anull[outa]")
    return "".join(parts)

def build_filter_complex_video_only(keeps: list[Segment], fps: float | None = None) -> str:
    parts: list[str] = []
    for i, seg in enumerate(keeps):
        v_chain = f"[0:v]trim=start={seg.start}:end={seg.end},setpts=PTS-STARTPTS"
        if fps and fps > 1.0:
            v_chain += f",fps={fps:.3f}"
        parts.append(f"{v_chain}[v{i}];")
    concat_inputs = "".join([f"[v{i}]" for i in range(len(keeps))])
    parts.append(f"{concat_inputs}concat=n={len(keeps)}:v=1:a=0[outv]")
    return "".join(parts)

def build_filter_complex_multi(
    keeps: list[Segment],
    input_count: int,
    layout: str,
    fps: float | None = None,
    force_audio_async: bool = True,
) -> str:
    """
    Build a filter_complex for multiple inputs:
      - concat video sequentially (input order)
      - concat audio sequentially (input order)
      - apply keeps to the concatenated video and audio
    """
    del layout
    n_inputs = max(1, int(input_count))
    if n_inputs == 1:
        return build_filter_complex(keeps, fps=fps, force_audio_async=force_audio_async)

    parts: list[str] = []

    # --- Video: normalize size vs input 0, then concat sequentially
    parts.append("[0:v]setpts=PTS-STARTPTS,format=yuv420p[v0]")
    split_labels = ["v0c"] + [f"v0r{i}" for i in range(1, n_inputs)]
    parts.append(
        f"[v0]split={len(split_labels)}" + "".join(f"[{l}]" for l in split_labels)
    )
    v_inputs = ["v0c"]
    for j in range(1, n_inputs):
        parts.append(f"[{j}:v]setpts=PTS-STARTPTS,format=yuv420p[v{j}]")
        parts.append(f"[v{j}][v0r{j}]scale2ref=iw:ih[v{j}s][v0d{j}]")
        v_inputs.append(f"v{j}s")
    parts.append("".join(f"[{v}]" for v in v_inputs) + f"concat=n={n_inputs}:v=1:a=0[vcat]")

    # --- Audio: concat sequentially to match video timeline
    for j in range(n_inputs):
        parts.append(f"[{j}:a]asetpts=PTS-STARTPTS[a{j}]")
    ainputs = "".join(f"[a{j}]" for j in range(n_inputs))
    parts.append(f"{ainputs}concat=n={n_inputs}:v=0:a=1[acat]")

    # --- Apply keeps to vcat/aout
    v_src = [f"vsrc{i}" for i in range(len(keeps))]
    a_src = [f"asrc{i}" for i in range(len(keeps))]
    parts.append(f"[vcat]split={len(keeps)}" + "".join(f"[{l}]" for l in v_src))
    parts.append(f"[acat]asplit={len(keeps)}" + "".join(f"[{l}]" for l in a_src))

    for i, seg in enumerate(keeps):
        v_chain = f"[{v_src[i]}]trim=start={seg.start}:end={seg.end},setpts=PTS-STARTPTS"
        if fps and fps > 1.0:
            v_chain += f",fps={fps:.3f}"
        parts.append(f"{v_chain}[v{i}]")
        parts.append(
            f"[{a_src[i]}]atrim=start={seg.start}:end={seg.end},asetpts=PTS-STARTPTS[a{i}]"
        )

    concat_inputs = "".join([f"[v{i}][a{i}]" for i in range(len(keeps))])
    parts.append(f"{concat_inputs}concat=n={len(keeps)}:v=1:a=1[outv][outa0]")
    if force_audio_async:
        parts.append("[outa0]aresample=async=1:first_pts=0[outa]")
    else:
        parts.append("[outa0]anull[outa]")

    return ";".join(parts)
