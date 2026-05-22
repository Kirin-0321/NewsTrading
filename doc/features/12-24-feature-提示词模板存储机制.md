# 提示词模板存储机制说明

## 🤔 用户常见疑问

**问题**：为什么提示词模板需要两个文件存储？在软件中新建的模板是否会生效？

**答案**：完全生效！✅ 让我详细解释这个设计。

---

## 📂 两个文件的作用

### 1️⃣ `core/ai_config.py`（代码文件）

**角色**：【出厂默认设置】

```python
def get_default_prompt_templates(self) -> Dict:
    """获取默认提示词模板"""
    return {
        "standard": {
            "name": "标准分析",
            "system_prompt": "...",
            "user_prompt_template": "..."
        },
        "aggressive": {...},
        "conservative": {...}
    }
```

**特点**：
- ✅ 只在**首次运行**时使用（创建初始配置）
- ✅ 配置文件**损坏时**使用（恢复出厂设置）
- ✅ 包含**最新优化的模板**（代码升级时更新）
- ❌ **日常使用时不会读取**

**类比**：就像手机的**ROM（只读存储器）**，存放出厂系统。

---

### 2️⃣ `config/ai_config.json`（配置文件）

**角色**：【用户实际使用的配置】

```json
{
  "current_provider": "deepseek",
  "prompt_templates": {
    "standard": {
      "name": "标准分析",
      "system_prompt": "...",
      "user_prompt_template": "..."
    },
    "custom_5": {
      "name": "我的自定义模板",
      "system_prompt": "...",
      "user_prompt_template": "..."
    }
  }
}
```

**特点**：
- ✅ 程序**运行时实际读取**的配置
- ✅ 所有**用户修改**都保存在这里
- ✅ **新建/编辑模板**直接写入此文件
- ✅ **立即生效**，无需重启

**类比**：就像手机的**用户数据存储**，存放你的所有设置。

---

## 🔄 完整工作流程

### 场景1：首次运行应用

```
1. 程序启动
   ↓
2. 检查 config/ai_config.json 是否存在
   ↓
3. 不存在 → 调用 get_default_config()
   ↓
4. 调用 get_default_prompt_templates()
   ↓
5. 创建 config/ai_config.json
   ↓
6. 写入默认模板（standard, aggressive, conservative, value, short_term）
   ↓
7. 用户可以使用这些模板
```

**结果**：`config/ai_config.json` 包含了 `core/ai_config.py` 中的默认模板。

---

### 场景2：日常使用（已有配置文件）

```
1. 程序启动
   ↓
2. 检查 config/ai_config.json 是否存在
   ↓
3. 存在 → 直接读取
   ↓
4. 加载用户的所有自定义设置
   ↓
5. core/ai_config.py 中的默认模板不再使用 ❌
```

**重点**：一旦有了 `config/ai_config.json`，程序就只读取这个文件，不再读取代码中的默认模板。

---

### 场景3：用户在软件中新建模板

```
1. 用户点击【新建模板】按钮
   ↓
2. 填写模板名称、system_prompt、user_prompt_template
   ↓
3. 点击【保存】
   ↓
4. 调用 config.save_prompt_template(template_key, data)
   ↓
5. 保存到 self.config['prompt_templates']
   ↓
6. 调用 self.save_config()
   ↓
7. 写入 config/ai_config.json ✅
   ↓
8. 刷新模板下拉列表
   ↓
9. 新模板立即可用！
```

**代码证据**：

```python
# gui/pages/ai_analysis_page.py (857-882行)
def new_template(self):
    """新建模板"""
    try:
        config = AIConfig()
        config.save_prompt_template(template_key, data)  # ← 保存
        self.load_template_list()  # ← 刷新列表
        QMessageBox.information(self, "成功", "新模板已创建")
    except Exception as e:
        QMessageBox.critical(self, "错误", f"新建模板失败: {str(e)}")
```

```python
# core/ai_config.py (540-545行)
def save_prompt_template(self, template_name: str, template_data: Dict):
    """保存提示词模板"""
    self.config['prompt_templates'][template_name] = template_data
    self.save_config()  # ← 写入 config/ai_config.json
```

**结论**：在软件中新建的模板会**立即保存到 `config/ai_config.json`**，并**立即生效**！

---

### 场景4：编辑现有模板

```
1. 用户选择某个模板
   ↓
2. 点击【编辑模板】按钮
   ↓
3. 修改 system_prompt 或 user_prompt_template
   ↓
4. 点击【保存】
   ↓
5. 调用 config.save_prompt_template(template_key, new_data)
   ↓
6. 覆盖 config/ai_config.json 中的对应模板 ✅
   ↓
7. 修改立即生效！
```

**结论**：编辑模板也是**直接修改 `config/ai_config.json`**，立即生效。

---

## ✅ 验证方法

### 方法1：通过文件内容验证

**步骤**：

1. **打开** `config/ai_config.json`
2. **记下**当前有几个模板（例如5个）
3. **在软件中**新建一个模板，命名为"测试模板"
4. **再次打开** `config/ai_config.json`
5. **查找** `"custom_6": { "name": "测试模板" }`

**预期结果**：✅ 应该能看到新增的模板。

---

### 方法2：通过模板列表验证

**步骤**：

1. **在AI分析页面**，查看提示词模板下拉列表
2. **记下**当前有几个模板
3. **点击【新建模板】**，创建一个新模板
4. **保存成功后**，查看下拉列表

**预期结果**：✅ 下拉列表中应该立即显示新模板。

---

### 方法3：通过AI分析验证

**步骤**：

1. **新建一个模板**，在 system_prompt 中加入特殊标记，例如：
   ```
   你是投资分析师。【这是我的自定义模板测试】
   ```
2. **保存模板**
3. **选择这个新模板**
4. **运行AI分析**
5. **查看AI的回复**

**预期结果**：✅ AI的回复风格应该符合你自定义的 system_prompt。

---

## 🎯 核心结论

| 问题 | 答案 |
|-----|-----|
| 在软件中新建的模板会保存吗？ | ✅ 会保存到 `config/ai_config.json` |
| 保存后需要重启软件吗？ | ❌ 不需要，立即生效 |
| 会覆盖代码中的默认模板吗？ | ❌ 不会，它们是独立的 |
| 新建的模板会在下次启动时丢失吗？ | ❌ 不会，永久保存 |
| 今天优化的默认模板会影响已创建的模板吗？ | ❌ 不会，除非删除 `config/ai_config.json` |

---

## 💡 为什么需要两个文件？

### 设计理念：分离关注点

**代码中的默认模板**（`core/ai_config.py`）：
- 📦 **版本控制**：跟随代码升级而更新
- 🔧 **开发维护**：开发者可以优化默认模板
- 🚀 **新用户友好**：提供开箱即用的优质模板

**配置文件中的用户模板**（`config/ai_config.json`）：
- 💾 **用户数据**：存储用户的个性化设置
- 🛡️ **数据安全**：不会因代码升级而丢失
- ✏️ **灵活修改**：用户可以随意增删改

### 类比：手机系统

```
📱 手机系统 = core/ai_config.py（代码）
   ├─ ROM（只读存储器）
   ├─ 出厂默认设置
   ├─ 系统升级时更新
   └─ 用户无法直接修改

💾 用户数据 = config/ai_config.json（配置文件）
   ├─ 用户可写存储
   ├─ 所有个性化设置
   ├─ 可以随时修改
   └─ 恢复出厂设置时会被重置
```

### 优势

| 优势 | 说明 |
|-----|-----|
| ✅ 用户自由 | 可以任意修改、新建、删除模板 |
| ✅ 数据安全 | 用户配置不会因升级而丢失 |
| ✅ 容易恢复 | 配置损坏时可以恢复默认 |
| ✅ 持续优化 | 开发者可以不断优化默认模板 |
| ✅ 向后兼容 | 老用户的自定义模板不受影响 |

---

## 🔍 深入理解：配置文件的优先级

### 读取顺序

```
程序启动
  ↓
检查 config/ai_config.json 是否存在？
  ├─ 存在 → 读取 config/ai_config.json ✅（用户配置）
  └─ 不存在 → 读取 core/ai_config.py 中的默认配置（出厂设置）
                ↓
             创建 config/ai_config.json
                ↓
             以后都读取 config/ai_config.json ✅
```

### 优先级规则

1. **存在 `config/ai_config.json`**：
   - ✅ 使用配置文件中的所有设置
   - ❌ 忽略代码中的默认设置

2. **不存在 `config/ai_config.json`**：
   - ✅ 使用代码中的默认设置
   - ✅ 创建配置文件
   - ✅ 以后使用配置文件

### 今天的优化影响谁？

| 用户类型 | 影响 | 原因 |
|---------|-----|-----|
| 新用户（首次运行） | ✅ 立即生效 | 会创建新的 `config/ai_config.json`，包含优化后的模板 |
| 老用户（已有配置） | ❌ 不会自动更新 | 程序读取的是已有的 `config/ai_config.json` |
| 老用户想要新模板 | ✅ 手动更新 | 可以删除 `config/ai_config.json`，重启后重新生成 |

---

## 🛠️ 实用技巧

### 技巧1：恢复优化后的默认模板

如果您是老用户，想要使用今天优化后的默认模板：

**方法1：备份后删除配置文件**

```bash
# 1. 备份当前配置（保存自定义模板）
copy config\ai_config.json config\ai_config.json.backup

# 2. 删除配置文件
del config\ai_config.json

# 3. 重启应用
# 程序会自动创建新的配置文件，包含优化后的模板

# 4. 如果需要恢复自定义模板
# 从备份文件中复制 "custom_" 开头的模板到新文件
```

**方法2：手动更新特定模板**

1. 打开 `config/ai_config.json`
2. 找到 `"standard": {...}` 部分
3. 从 `core/ai_config.py` 中复制优化后的内容
4. 粘贴并保存

---

### 技巧2：在多台电脑间同步模板

**场景**：您在办公室电脑上创建了很多自定义模板，想在家里的电脑上也使用。

**方法**：

```bash
# 在办公室电脑上
# 复制 config\ai_config.json 到U盘或云盘

# 在家里的电脑上
# 将文件复制到项目的 config\ 目录下
copy U:\ai_config.json F:\爬虫\config\ai_config.json

# 重启应用即可
```

---

### 技巧3：创建模板库

**场景**：您有多套投资策略，想为每套策略创建独立的配置文件。

**方法**：

```bash
# 为不同策略创建不同的配置文件
config\ai_config_激进.json
config\ai_config_稳健.json
config\ai_config_价值.json

# 使用时，重命名为 ai_config.json
copy config\ai_config_激进.json config\ai_config.json
```

---

### 技巧4：模板版本控制

**场景**：您想记录模板的修改历史。

**方法**：

```bash
# 每次修改前备份
copy config\ai_config.json config\backups\ai_config_20251223.json

# 或使用Git
git add config\ai_config.json
git commit -m "优化标准分析模板，加入新闻分级"
```

---

## ❓ 常见问题

### Q1：如果我直接修改 `core/ai_config.py`，会生效吗？

**A**：不会（对于已有配置文件的用户）。

- ❌ 如果 `config/ai_config.json` 已存在，修改代码不会生效
- ✅ 只有新用户（首次运行）会使用代码中的默认模板
- 💡 如果想让修改生效，需要删除 `config/ai_config.json` 并重启

---

### Q2：我能同时修改两个文件吗？

**A**：可以，但建议只修改一个。

- ✅ **推荐**：只修改 `config/ai_config.json`（用户配置）
- ⚠️ **不推荐**：修改 `core/ai_config.py`（代码文件）
  - 原因：代码升级时会被覆盖
  - 除非你是开发者，想要更新默认模板

---

### Q3：删除 `config/ai_config.json` 有什么后果？

**A**：相当于"恢复出厂设置"。

- ❌ 你的所有自定义模板会丢失
- ❌ API密钥等配置会被清空
- ✅ 重启后会获得最新的默认模板
- 💡 建议：删除前先备份

---

### Q4：为什么我的自定义模板没有新闻分级功能？

**A**：因为你的模板是在今天优化之前创建的。

**解决方法**：
1. 点击【编辑模板】
2. 在 `system_prompt` 中加入新闻分级标准（参考默认模板）
3. 在 `user_prompt_template` 中加入分析流程
4. 保存

---

### Q5：我可以删除默认模板吗？

**A**：在配置文件中可以，但不建议。

```json
{
  "prompt_templates": {
    "standard": {...},      // 可以删除
    "aggressive": {...},    // 可以删除
    "custom_5": {...}       // 可以删除
  }
}
```

- ✅ 技术上可以删除任何模板
- ⚠️ 但删除后无法通过界面恢复
- 💡 建议：隐藏而不是删除

---

## 📞 技术支持

如有疑问：

1. 查看配置文件：`config/ai_config.json`
2. 查看代码文件：`core/ai_config.py`
3. 查看日志：寻找 "加载配置" 相关信息

---

## 📚 相关文档

- [提示词模板-新闻分级说明](./提示词模板-新闻分级说明.md)
- [AI分析模块-使用指南](./AI分析模块-使用指南.md)

---

**更新日期**：2025年12月23日  
**版本**：v2.0

