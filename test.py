# %%
import numpy as np
from typing import Literal
import cv2
from xpra_screenshot import capture_png
import matplotlib.pyplot as plt

Stage = Literal["init", "flow", "tab"]

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
        self.list_icon = to_ch4(cv2.imread("./list_icon.png"))
        self.profile_icon = to_ch4(cv2.imread("./profile_icon.png"))

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
        img = self.bin_img
        for _ in range(len(self.split_line_idxs)):
            idx = self.split_line_idxs[-1]
            if idx > 800:
                # 大于 800 还有分割线的话，大概就是最右侧那个
                img = img[:, :idx+1]
                self.split_line_idxs.pop()

        self.icon = (img[42:42+19, -19-22:-22])
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
        if self.icon is None:
            raise ValueError("Icon not found yet. Call find_icon() first.")
        return np.mean(self.icon == self.list_icon) > 0.9
    
    @property
    def has_profile_icon(self):
        if self.icon is None:
            raise ValueError("Icon not found yet. Call find_icon() first.")
        return np.mean(self.icon == self.profile_icon) > 0.9

img = capture_png()
print(img.shape)
if img.shape[-1] == 3:
    img = np.concatenate([img, np.ones((*img.shape[:2], 1), dtype=np.uint8)*255], axis=2)

# img = iio.imread("./01.init.png")


plt.figure(figsize=(20, 10))
plt.imshow(img)

extractor = Extractor(img)
extractor.find_split_line()
# icon = extractor.find_icon()
# print('list' if extractor.has_list_icon else '', 'profile' if extractor.has_profile_icon else '')
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
