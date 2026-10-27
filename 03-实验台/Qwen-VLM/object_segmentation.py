"""Class-independent foreground proposals for a fixed tabletop camera.

An empty-table reference handles neutral objects; without one, saturated
colors are the deliberately limited fallback. No object labels are trained.
"""
import cv2
import numpy as np


def region_mask(shape, polygon=None):
    height, width = shape[:2]
    mask = np.full((height, width), 255, np.uint8)
    if polygon is not None:
        mask.fill(0)
        vertices = np.rint(np.array(polygon) * (width - 1, height - 1)).astype(np.int32)
        cv2.fillPoly(mask, [vertices], 255)
    return mask


def separate_objects(frame, polygon=None, background=None, threshold=30,
                     min_area=0.3, max_objects=20):
    """Return proposals in rotated full-frame pixels and a foreground mask.

    min_area is a percentage of ROI area. Background must have the same pose,
    resolution and illumination. Touching objects can remain one proposal.
    """
    if background is not None and background.shape != frame.shape:
        raise ValueError('背景尺寸与当前画面不同，请重新采集空台面。')
    allowed = region_mask(frame.shape, polygon)
    # Keep selection edges and the frame boundary from becoming candidates.
    inner = cv2.erode(allowed, np.ones((5, 5), np.uint8), borderType=cv2.BORDER_CONSTANT, borderValue=0)
    smooth = cv2.GaussianBlur(frame, (5, 5), 0)
    if background is not None:
        current = cv2.cvtColor(smooth, cv2.COLOR_BGR2LAB).astype(np.float32)
        reference = cv2.cvtColor(cv2.GaussianBlur(background, (5, 5), 0), cv2.COLOR_BGR2LAB).astype(np.float32)
        # Median removes modest global illumination drift; discount luminance
        # changes to reduce shadows while retaining neutral-object boundaries.
        difference = current - reference
        pixels = difference[inner > 0]
        if not len(pixels):
            return [], np.zeros(frame.shape[:2], np.uint8)
        difference -= np.median(pixels, axis=0)
        difference[:, :, 0] *= 0.65
        score = np.linalg.norm(difference, axis=2)
        binary = np.uint8(score >= threshold) * 255
    else:
        hsv = cv2.cvtColor(smooth, cv2.COLOR_BGR2HSV)
        binary = np.uint8((hsv[:, :, 1] >= threshold) & (hsv[:, :, 2] >= 35)) * 255
    binary = cv2.bitwise_and(binary, inner)
    binary = split_touching(binary)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    binary = cv2.bitwise_and(binary, inner)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cutoff = max(40, cv2.countNonZero(allowed) * min_area / 100)
    candidates = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < cutoff:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if w < 8 or h < 8:
            continue
        # An object cut by ROI is retained with a flag instead of silently lost.
        contact = np.zeros_like(binary)
        cv2.drawContours(contact, [contour], -1, 255, 2)
        edge = cv2.subtract(inner, cv2.erode(inner, np.ones((5, 5), np.uint8)))
        candidates.append({'bbox': [x, y, w, h], 'contour': contour,
                           'area': round(area, 1),
                           'touches_roi': bool(cv2.countNonZero(cv2.bitwise_and(contact, edge)))})
    # Bound workload, but never silently omit additional objects.
    if len(candidates) > max_objects:
        raise ValueError(f'检测到 {len(candidates)} 个候选（上限 {max_objects}）；请调整阈值、最小面积或选区。')
    candidates.sort(key=lambda obj: (obj['bbox'][0], obj['bbox'][1]))
    for index, obj in enumerate(candidates, 1):
        obj['object_id'] = index
    return candidates, binary


def split_touching(binary):
    """Split blobs whose objects touch, without learning object classes."""
    if cv2.countNonZero(binary) == 0:
        return binary
    distance = cv2.distanceTransform(binary, cv2.DIST_L2, 5)
    peak = float(distance.max())
    if peak < 8:
        return binary
    # One peak per substantial object; shallow peaks are intentionally ignored
    # so table shadows do not become markers.
    sure = np.uint8(distance >= max(8, peak * .32)) * 255
    count, markers = cv2.connectedComponents(sure)
    if count <= 2:
        return binary
    unknown = cv2.subtract(binary, sure)
    markers = markers + 1
    markers[unknown > 0] = 0
    color = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
    cv2.watershed(color, markers)
    result = np.zeros_like(binary)
    result[markers > 1] = 255
    return result


def object_crop(frame, candidate, polygon=None, max_side=1280):
    """Isolate this contour, hide other objects, then add a small white margin."""
    mask = np.zeros(frame.shape[:2], np.uint8)
    cv2.drawContours(mask, [candidate['contour']], -1, 255, cv2.FILLED)
    # A slight expansion keeps contour-edge pixels and anti-aliased details.
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8))
    mask = cv2.bitwise_and(mask, region_mask(frame.shape, polygon))
    isolated = np.full_like(frame, 255)
    isolated[mask > 0] = frame[mask > 0]
    x, y, w, h = candidate['bbox']
    padding = max(8, round(max(w, h) * .08))
    left, top = max(0, x-padding), max(0, y-padding)
    right, bottom = min(frame.shape[1], x+w+padding), min(frame.shape[0], y+h+padding)
    crop = isolated[top:bottom, left:right]
    scale = min(1, max_side / max(crop.shape[:2]))
    return cv2.resize(crop, (max(1, round(crop.shape[1]*scale)), max(1, round(crop.shape[0]*scale))))


def annotate(frame, candidates):
    output = frame.copy()
    for obj in candidates:
        x, y, w, h = obj['bbox']
        cv2.drawContours(output, [obj['contour']], -1, (0, 220, 255), 2)
        center = obj.get('center_px', [x+(w-1)/2, y+(h-1)/2])
        cv2.drawMarker(output, tuple(round(v) for v in center), (0,0,255), cv2.MARKER_CROSS, 14, 2)
        cv2.rectangle(output, (x, y), (x+w, y+h), (0, 220, 255), 2)
        cv2.putText(output, f"#{obj['object_id']}", (x, max(20, y-7)),
                    cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 80, 230), 2)
    return output
