"""Independent re-audit and candidate selection; never reuse old observations."""

from pathlib import Path


def review_after(
    analyzer, before, image, prompt, observations, previous=None, following=None
):
    if not before or before.get("decision") == "SKIP":
        raise ValueError("请先审计一个生成镜头。")
    if image is None:
        raise ValueError("请上传重跑后的图片。")
    if before.get("canon_version") != analyzer.canon.data["meta"]["version"]:
        raise ValueError("Canon 已变更，请先按当前版本重新审计改前图片。")
    after = analyzer.audit(
        before["shot_id"],
        before["mode"],
        prompt,
        observations or [],
        previous,
        image,
        following,
    )
    old = {i["rule_id"] for i in before["issues"]}
    new = {i["rule_id"] for i in after["issues"]}
    comparison = [
        [label, r["decision"], r["score"], r["human_review_count"], r["canon_version"]]
        for label, r in [("改前", before), ("改后", after)]
    ]
    verified = after["api_reviewed"] or after["decision"] == "PASS"
    status = (
        "本次复审未再报告：" + ", ".join(sorted(old - new))
        if verified and old - new
        else "尚无足够证据确认旧问题已消除。"
    )
    return (
        after,
        comparison,
        status + " 未再报告不等于已证明动作、时序或所有遮挡区域正确。",
    )


def rank_takes(analyzer, shot, mode, prompt, files, previous=None, following=None):
    if not files:
        raise ValueError("请上传候选图片。")
    if len(files) > 12:
        raise ValueError("每批最多 12 张；多模态模式每张都会调用一次 API。")
    results = []
    for item in files:
        path = str(item)
        try:
            result = analyzer.audit(shot, mode, prompt, [], previous, path, following)
            result["filename"] = Path(path).name
            results.append(result)
        except (OSError, ValueError, TypeError) as exc:
            results.append(
                {
                    "filename": Path(path).name,
                    "decision": "ERROR",
                    "score": None,
                    "needs_human_review": True,
                    "api_reviewed": False,
                    "error": str(exc),
                }
            )
    results.sort(key=lambda r: -(r["score"] if r["score"] is not None else -1))
    eligible = [
        r
        for r in results
        if r["decision"] == "PASS"
        and r.get("api_reviewed")
        and not r["needs_human_review"]
    ]
    best = eligible[0] if eligible else None
    rows = [
        [
            i + 1,
            r["filename"],
            r["decision"],
            r["score"],
            "是" if r["needs_human_review"] else "否",
            "推荐（可见范围）" if r is best else "",
            r.get("error") or r.get("api_error") or "",
        ]
        for i, r in enumerate(results)
    ]
    summary = (
        f"推荐：{best['filename']}。同分按上传顺序；仍需导演确认整镜。"
        if best
        else "本批没有可自动推荐的 PASS。离线模式只提供颜色等粗筛，不能可靠选择最佳 Take。"
    )
    return summary, rows, results
