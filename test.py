# %%
import numpy as np
from typing import Literal
import cv2
from x11_wechat import capture_wechat_png, move_wechat_mouse
import matplotlib.pyplot as plt
import time
from pathlib import Path

Stage = Literal["init", "flow", "tab"]
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

def to_ch4(img: np.ndarray) -> np.ndarray:
    if img.shape[-1] == 3:
        img = np.concatenate([img, np.ones((*img.shape[:2], 1), dtype=np.uint8)*255], axis=2)
    return img



class Extractor:
    def __init__(self, raw_img: np.ndarray):
        self.raw_img = raw_img
        self.bin_img = (raw_img*1.0 - 225 > 0).astype(np.uint8)*255
        self.split_line_idxs: list[int] = []
        self.icon: np.ndarray | None = None
        self.icon_name: Literal["list", "profile"] | None = None
        self.icon_score: float = 0
        self.list_icon = to_ch4(cv2.imread(str(TEMPLATE_DIR / "list_icon.png")))
        self.profile_icon = to_ch4(cv2.imread(str(TEMPLATE_DIR / "profile_icon.png")))

    def find_split_line(self):
        gray = self.bin_img.mean(axis=2)
        mean = gray.mean(axis=0)
        std = gray.std(axis=0)
        score = std * mean
        gross_idxs = np.where(score == 0)[0]
        idxs = []
        for idx in gross_idxs:
            if idx != 0 and (not idxs or idx - idxs[-1] > 10):
                idxs.append(int(idx))
        if idxs[0] != 64:
            import warnings
            warnings.warn(f"Expected first split line at 64, but found at {idxs[0]}. This may cause issues in later processing.")

        self.split_line_idxs = idxs
        return idxs

    def find_icon(self):
        roi_x = max(0, self.raw_img.shape[1] - 120)
        roi_y = 20
        roi = self.raw_img[roi_y:90, roi_x:]
        roi_gray = cv2.cvtColor(roi[..., :3], cv2.COLOR_RGB2GRAY)
        _, roi_dark = cv2.threshold(roi_gray, 210, 255, cv2.THRESH_BINARY_INV)

        best_name: Literal["list", "profile"] | None = None
        best_score = -1.0
        best_loc = (0, 0)
        best_shape = (0, 0)
        for name, template in [("list", self.list_icon), ("profile", self.profile_icon)]:
            template_gray = cv2.cvtColor(template[..., :3], cv2.COLOR_BGR2GRAY)
            _, template_dark = cv2.threshold(template_gray, 210, 255, cv2.THRESH_BINARY_INV)
            result = cv2.matchTemplate(roi_dark, template_dark, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(result)
            if score > best_score:
                best_name = name
                best_score = float(score)
                best_loc = loc
                best_shape = template.shape[:2]

        x, y = best_loc
        h, w = best_shape
        self.icon = roi[y:y+h, x:x+w]
        self.icon_name = best_name
        self.icon_score = best_score
        return self.icon

    def extract_account_list_img(self) -> np.ndarray | None:
        if not self.split_line_idxs:
            raise ValueError("Split line not found yet. Call find_split_line() first.")
        if len(self.split_line_idxs) < 3:
            return None
        return self.raw_img[:, self.split_line_idxs[1]:self.split_line_idxs[2]]

    def find_unread_subs(self) -> list[tuple[int, int]]:
        account_list_img = self.extract_account_list_img()
        if account_list_img is None:
            return []
        mask = (account_list_img == np.array([250, 81, 81, 255])).all(axis=2)
        _, binary = cv2.threshold(mask.astype(np.uint8)*255, 10, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=lambda c: cv2.boundingRect(c)[0])

        subs: list[tuple[int, int]] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            mini = account_list_img[y:y+h, x:x+w]
            score = (mini[..., :3].std(2).mean() * 3)

            if score > 200:
                subs.append((x + self.split_line_idxs[1], y))
        return subs

    @property
    def has_list_icon(self):
        if self.icon_name is None:
            raise ValueError("Icon not found yet. Call find_icon() first.")
        return self.icon_name == "list"
    
    @property
    def has_profile_icon(self):
        if self.icon_name is None:
            raise ValueError("Icon not found yet. Call find_icon() first.")
        return self.icon_name == "profile"

img = capture_wechat_png()
print(img.shape)
if img.shape[-1] == 3:
    img = np.concatenate([img, np.ones((*img.shape[:2], 1), dtype=np.uint8)*255], axis=2)

# img = iio.imread("./01.init.png")


plt.figure(figsize=(20, 10))
plt.imshow(img)

extractor = Extractor(img)
cv2.imwrite("bin.png", cv2.cvtColor(extractor.bin_img, cv2.COLOR_RGBA2BGRA))
extractor.find_split_line()
icon = extractor.find_icon()
print(icon.shape)
cv2.imwrite("icon.png", cv2.cvtColor(icon, cv2.COLOR_RGBA2BGRA))
print('list' if extractor.has_list_icon else '', 'profile' if extractor.has_profile_icon else '')
for i in extractor.split_line_idxs:
    plt.axvline(i, 0, img.shape[0], color='r', linestyle='--')
print(extractor.split_line_idxs)
# %%
unread_subs = extractor.find_unread_subs()
for x, y in unread_subs:
    plt.plot(x+10, y+10, 'ro')
# %%
plt.savefig("score.png")
# %%
print(f'{extractor.find_unread_subs()=}')

if len(unread_subs) >= 2:
    first_x, first_y = unread_subs[0]
    second_x, second_y = unread_subs[1]
    move_wechat_mouse(first_x + 10, first_y + 10)
    time.sleep(2)
    move_wechat_mouse(second_x + 10, second_y + 10)
