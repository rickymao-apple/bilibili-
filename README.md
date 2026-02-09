
## 安装依赖（Mac）

```bash
python3 -m pip install --upgrade pip
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
brew install ffmpeg
```

## 运行

```bash
python3 main.py
```
📘 项目简介：为什么要做这个工具？ 很多高中生在 B 站上听网课，但现实情况是： 周末时间有限，大量课程根本听不完 学校不允许使用电子设备，无法在课堂或晚自习继续学 想补知识，却只能挤在零碎时间听视频，效率非常低 为了帮助这些学生，我开发了这个 「B 站视频一键转文字」工具： 输入一个 B 站视频链接，本工具会自动： 下载视频 提取音频 用 FunASR 进行语音识别 输出可打印的 文字稿（含标点） 这样学生就可以： 把网课内容 打印出来，带到学校随时看 随手做笔记、划重点 复习效率比听视频快几倍 周末不用被大量未听完的视频压着喘不过气 这是一个为 真正需要高效复习的学生 而生的小工具，希望能让大家的学习轻松一点。


更新提醒：如果funsar下载不了就换成国内的源（清华源：pip install funasr -i https://pypi.tuna.tsinghua.edu.cn/simple）


## 求助
现在转换完文字之后，我本来想喂给ai做成思维导图，但是api现在不好调用，没有钱买付费的api，有没有免费的api可以支援一下？有什么优化的点希望看到的技术大哥帮我优化一下，本人高中生，代码基础不是很深，多多指教。
