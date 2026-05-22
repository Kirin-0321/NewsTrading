# 火山引擎豆包大模型集成说明

## 📚 概述

火山引擎豆包大模型是字节跳动推出的企业级AI大模型服务，通过火山方舟平台提供。本系统已集成豆包大模型，支持海量新闻数据分析。

**官方文档**: https://www.volcengine.com/docs/82379/1399008

---

## 🚀 核心优势

### 1. **豆包1.6版本 (doubao-seed-1-6-251015)**
- ✅ **最新版本**：2025年10月15日发布
- ✅ **长上下文**：支持超长文本输入
- ✅ **高性能**：专为企业级应用优化
- ✅ **稳定可靠**：字节跳动自研模型

### 2. **多版本支持**
| 模型 | 上下文长度 | 适用场景 | 推荐度 |
|------|-----------|---------|--------|
| **doubao-seed-1-6-251015** | 长上下文 | 大规模新闻分析 | ⭐⭐⭐⭐⭐ |
| doubao-pro-32k | 32K | 通用分析 | ⭐⭐⭐⭐☆ |
| doubao-pro-128k | 128K | 长文本分析 | ⭐⭐⭐⭐☆ |
| doubao-lite-32k | 32K | 快速分析 | ⭐⭐⭐☆☆ |
| doubao-lite-128k | 128K | 经济型长文本 | ⭐⭐⭐☆☆ |

---

## 🔧 配置步骤

### 步骤1: 获取API Key

1. 访问 [火山方舟平台](https://console.volcengine.com/ark)
2. 登录/注册账号
3. 进入"大模型服务平台"
4. 创建应用并获取 API Key
5. 记录您的 API Key（格式：`ark-xxx`）

### 步骤2: 在系统中配置

1. **打开应用**，进入"AI分析"页面

2. **选择服务商**：
   - 服务商下拉框选择：**火山引擎**

3. **配置API Key**：
   - 在"API Key"输入框中粘贴您的 API Key
   - 格式示例：`ark-xxxxxxxxxxxxxxxx`

4. **选择模型**（推荐）：
   - **doubao-seed-1-6-251015** （最新版，长上下文）

5. **保存配置**：
   - 点击"保存配置"按钮
   - 系统会自动保存到 `config/ai_config.json`

6. **测试连接**：
   - 点击"测试连接"按钮
   - 确保配置正确

---

## 📊 使用示例

### 示例1: 2000条新闻分析

```
服务商：火山引擎
模型：doubao-seed-1-6-251015
数据源：导出文件
最大板块数：6
每板块股票数：5
最大新闻数：2000
提示词模板：标准分析
```

**优势**：
- ✅ 长上下文支持，可处理大量新闻
- ✅ 分析质量高，逻辑严谨
- ✅ 响应速度快

### 示例2: 结合盘后总结

```
服务商：火山引擎
模型：doubao-seed-1-6-251015
盘后总结：今日A股三大指数集体收涨...（输入您的总结）
```

**优势**：
- ✅ 结合市场实际情况
- ✅ 提供更精准的分析
- ✅ 增强投资决策价值

---

## 💰 价格对比

| 服务商 | 输入价格 | 输出价格 | 性价比 |
|-------|---------|---------|--------|
| OpenAI GPT-4 | 较高 | 较高 | ⭐⭐⭐☆☆ |
| DeepSeek V3.2 | 低 | 低 | ⭐⭐⭐⭐⭐ |
| **火山引擎豆包** | **中等** | **中等** | **⭐⭐⭐⭐☆** |
| 智谱GLM-4 | 中等 | 中等 | ⭐⭐⭐⭐☆ |

**注**：具体价格请参考 [火山方舟定价](https://www.volcengine.com/docs/82379/pricing)

---

## 🎯 最佳实践

### 1. **推荐配置**

```json
{
  "provider": "volcengine",
  "model": "doubao-seed-1-6-251015",
  "max_tokens": 4000,
  "temperature": 0.7,
  "max_news": 2000
}
```

### 2. **适用场景**

✅ **最适合**：
- 大批量新闻分析（1000-2000条）
- 需要长上下文理解的场景
- 企业级稳定性要求
- 对字节跳动生态友好的用户

⚠️ **不太适合**：
- 追求极致性价比（DeepSeek更优）
- 需要顶级推理能力（DeepSeek-reasoner更强）
- 预算极其有限的场景

### 3. **优化建议**

1. **温度参数**：
   - 标准分析：0.7（推荐）
   - 保守分析：0.5
   - 激进分析：0.9

2. **新闻数量**：
   - doubao-seed-1-6-251015：可处理2000+条新闻
   - doubao-pro-128k：建议1500条以内
   - doubao-lite：建议1000条以内

3. **提示词模板**：
   - 豆包模型对中文理解优秀
   - 建议使用"标准分析"或自定义模板
   - 可充分利用盘后总结功能

---

## 🔍 技术细节

### API接口

火山引擎使用 **OpenAI 兼容接口**：

```python
from openai import OpenAI

client = OpenAI(
    api_key="ark-xxx",
    base_url="https://ark.cn-beijing.volces.com/api/v3"
)

response = client.chat.completions.create(
    model="doubao-seed-1-6-251015",
    messages=[
        {"role": "system", "content": "系统提示词"},
        {"role": "user", "content": "用户提示词"}
    ],
    stream=True
)
```

### 配置文件

配置会自动保存到 `config/ai_config.json`：

```json
{
  "providers": {
    "volcengine": {
      "api_key": "ark-xxx",
      "base_url": "https://ark.cn-beijing.volces.com/api/v3",
      "model": "doubao-seed-1-6-251015",
      "max_tokens": 4000,
      "temperature": 0.7
    }
  }
}
```

---

## ❓ 常见问题

### Q1: 如何获取火山引擎API Key？
**A**: 访问 [火山方舟控制台](https://console.volcengine.com/ark)，创建应用后即可获取。

### Q2: 豆包模型与DeepSeek哪个更好？
**A**: 
- **DeepSeek V3.2**：性价比最高，推理能力最强，长文本优化
- **豆包1.6**：字节跳动自研，企业级稳定性，中文理解优秀
- **建议**：优先使用DeepSeek，豆包作为备选

### Q3: 豆包模型支持多长的上下文？
**A**: 
- `doubao-seed-1-6-251015`：长上下文版本，具体长度请查看官方文档
- `doubao-pro-128k`：128K tokens
- `doubao-lite-128k`：128K tokens

### Q4: 如何切换不同的豆包模型？
**A**: 在"AI分析"页面的"模型"下拉框中选择即可，系统支持：
- doubao-seed-1-6-251015（推荐）
- doubao-pro-32k
- doubao-pro-128k
- doubao-lite-32k
- doubao-lite-128k

### Q5: 豆包模型支持流式输出吗？
**A**: 是的，系统已实现流式输出，可实时查看分析进度。

---

## 📞 技术支持

### 火山引擎官方支持
- 文档：https://www.volcengine.com/docs/82379/1399008
- 控制台：https://console.volcengine.com/ark
- 社区：https://www.volcengine.com/community

### 系统相关问题
- 查看日志：`logs/` 目录
- 配置文件：`config/ai_config.json`
- 技术文档：`doc/` 目录

---

## 🎉 总结

火山引擎豆包大模型已成功集成到系统中，您现在可以：

✅ 选择"火山引擎"作为AI服务商
✅ 使用最新的 doubao-seed-1-6-251015 模型
✅ 处理大批量新闻数据（2000+条）
✅ 享受企业级的稳定性和性能
✅ 获得优秀的中文理解能力

**推荐配置**：
- **首选**：DeepSeek V3.2（性价比最高）
- **备选**：火山引擎豆包1.6（企业级稳定性）
- **进阶**：DeepSeek-reasoner（极致推理能力）

---

*最后更新：2025年12月22日*
*版本：v1.0*

