# 给 agent 的配置指引（hermes-federated-search · 三期）

本文件是**唯一权威入口**：怎么直接改配置字符串完成聚合搜索的配置，以及改完之后
人能看到什么。人面入口是桌面端 Settings → Plugins → 「聚合搜索」（卡片式界面），
两者读写的是同一棵树。

## 怎么打开界面（含启用桌面半）

桌面 Hermes → 设置 → 技能与工具 → 插件 → 打开 hermes-federated-search（带
`.hermes-package.json` 的统一包，桌面半默认不启用，需在此 opt-in）→ 再进
设置 → 插件 →「聚合搜索」。条目未出现：⌘K →「Reload desktop plugins」。

## 怎么加一条 provider

①卡片页「＋ 添加线路」；②填 name 与 base_url（可选 search_path/query_param/auth_style）；
③Key 字段填**环境变量名**（`^[A-Z][A-Z0-9_]{0,127}$`，如 TAVILY_API_KEY；多 Key 一行一个）；
密钥的**值**走 设置 → Keys（写同名变量）。新建卡一律 `type: custom`；旧卡里缺 `type`
的行按 registered 语义读取（provider 端宿主解析凭据），界面不注入、不改写。

## 改哪棵树

唯一真源：

```yaml
plugins:
  entries:
    hermes-federated-search:
      settings:      # ← 改这里（Settings → Plugins 写的也是这里）
        backends: []
        timeout: 10
        max_results: 8
        min_backends: 1
        ranking_mode: auto
        pool_strategy: fill_first   # 全局缺省值；卡内 pool_strategy 覆盖它
        # ranker: { provider: …, model: …, prompt: … }   # LLM 排序；prompt＝重排提示词，写法见下
```

- 旧的 `web.federated` 段仍然可用：读链按**顶层键**合并，`settings` 的键覆盖
  `web.federated` 的同名键（`provider.py:137-179`）。UI **不改写**它，也不要求迁移。
- 未配置任何一侧 → 搜索报 `no search backends configured`（`provider.py:265-268`）。

## 最小可用示例（两条线路）

```yaml
plugins:
  entries:
    hermes-federated-search:
      settings:
        backends:
          - name: tavily
            type: custom
            base_url: https://api.tavily.example/search
            search_path: /search
            query_param: query
            auth_style: bearer
            credentials:            # 只写环境变量名
              - TAVILY_API_KEY
              - TAVILY_API_KEY_2
          - name: minimax
            type: custom
            api_key_env: MINIMAX_CN_API_KEY
            base_url: https://api.minimax.example/search
        min_backends: 1
        ranking_mode: auto
```

## credentials：只录环境变量名

- `credentials` 是**环境变量名**列表；匹配 `^[A-Z][A-Z0-9_]{0,127}$`。
- 粘贴密钥值会被拒绝：厂商 token 前缀、长随机串、`Bearer …` 等值形在配置校验处
  直接报错（`provider.py:205-251`），报错信息只给字段下标与规则，**不回显值**。
- 密钥的**值**只走标准凭据门：桌面端 **Settings → Keys**（底层 `PUT /api/env`），
  由宿主写入运行环境；`config.yaml` / `.env` 名称对照里只有名字，没有值。
- 单卡线路也可用 `api_key_env: <ENV 名>`（宿主解析自己的凭据）；多卡轮换必须
  `type: custom` + `credentials`（registered 线路会**忽略** `credentials`，
  `provider.py:300-303`）。

## `k` 语义与陷阱

- `k` = 参与聚合的线路数，**必须恰好等于 `backends` 的长度**。
- 不写 `k` 时默认 = `len(backends)`，这是 UI 永远产出的形态（界面不写 `k`）。
- 如果存量配置里显式写了 `k` 且与线路数不符，provider 会在校验时报
  `k=… but … backend(s) configured`；卡片界面会显示警示条，且**不会自动改写**
  `k`——请手动改成当前线路数（或删掉该键）。

## 超时与最少成功线路（语义）

- 超时（timeout）＝等**全部**线路返回的总时限；到期未返回的线路丢弃、已返回的线路
  凑部分结果。
- 最少成功线路（min_backends）＝本次「有结果返回」的线路数须 ≥N，不足则整次搜索
  失败；成功＝线路在时限内返回了有结果的响应。
- required 线路必须成功（任一 required 线路失败即整次失败；仅在文档约定，界面不单设
  控件）。

## 排序（ranker）与每卡 Key 轮换

- `ranking_mode`：`auto`＝按 ranker 配置（未配置→关键词；配置→LLM＋失败回退关键词）；
  `keyword`／`llm` 为强制模式。卡片页在 `auto`/`llm` 下显示「排序模型（ranker.model）」
  菜单（数据源＝桌面 host 的 `model.options`，目录不可用时菜单区给提示，可用下方字符串
  或 Agent settings 直接改键）。选择写入 `ranker.provider`／`ranker.model`；写回是
  **整树合并**：`provider`／`model`／`prompt` 为受管叶子（「重排提示词」框即写 `prompt`，
  清空＝删该叶回内置默认），其余既有键原样保留。
- `ranker` 读链：`settings.ranker` 优先；仅当 `settings.ranker` **整键**不存在时回退 legacy
  `web.federated.ranker`（子键不合并）。
  无现值时界面**零自动写入**（不会凭空创建 ranker 键）。
- 重排序的辅助调用**固定设置 `reasoning_effort=off`（关闭模型思考）**：排序是机械步骤，
  避免模型（如 CommandCode 上默认开启思考的 DeepSeek）把输出额度耗在思考上而超时回退；
  不接受该字段的 provider，由辅助客户端自动去掉该字段重试。
- 每卡「Key 轮换策略（`pool_strategy`，fill_first／round_robin／random）」＝写到**线路
  对象**内的 `pool_strategy`；「跟随全局」＝不写该键。全局 `pool_strategy`＝缺省值，
  仅在线路未给时生效（provider.py:721-723 → :970）；全局键保留读+写回，界面控件已
  移入每条线路卡内。
- 旧卡兼容：`api_key_env` 单键卡与 `credentials` 多键卡并存；`required`、未知扩展键
  原样往返；`type` 缺失的行不注入 `type`、不改写为 `custom`。

### 重排提示词（ranker.prompt）

- 键：`plugins.entries.hermes-federated-search.settings.ranker.prompt`（字符串）。
  legacy `web.federated.ranker.prompt` 仍可读——但仅当 `settings.ranker` 整键不存在；
  `settings.ranker` 一出现即整体覆盖 legacy `ranker`（读链按顶层键合并，子键不合并）。
- 语义：设置后作为重排调用的 system 提示词发送（其后由插件追加固定的输出要求
  «Return ONLY a JSON array … No other text.»）；未设置＝内置默认提示词。
  仅在 LLM 排序时生效（`ranking_mode: llm`，或 `auto` 且 `ranker.provider` 已设置）；
  `keyword` 模式忽略。提示词**必须是字符串**——写成数字/布尔会让该次 LLM 排序异常并回退关键词。
- 写法：只写偏好/规则（例「优先中文来源、优先官方文档」），不要写输出格式（插件固定追加）。
  多行用 YAML 块标量：

  ```yaml
  plugins:
    entries:
      hermes-federated-search:
        settings:
          ranker:
            provider: opencode-go
            model: deepseek-v4-flash
            prompt: |
              优先中文来源和官方文档。
              优先近 6 个月的结果。
  ```

- agent 写入（三条等价入口）：
  1) 直改 config.yaml 上述路径（支持块标量）；
  2) `hermes config set plugins.entries.hermes-federated-search.settings.ranker.prompt "优先中文来源"`——
     免 `--force`；多行值会落成带 `\n` 转义的引号串（语义相同）；纯数字/布尔样值会被强转，勿用；
  3) 恢复默认：`hermes config unset plugins.entries.hermes-federated-search.settings.ranker.prompt`
     ——只删该叶子，`provider`/`model`/`backends` 等兄弟键保留。
- 读入口：`hermes config get plugins.entries.hermes-federated-search.settings.ranker`；
  界面侧＝「聚合搜索」卡片页「重排提示词」框 / Agent settings 子页。
- 生效：下一次搜索即读新值（provider 每次现读 config.yaml，无需重启）。

## 改完之后，人能看到什么

1. **界面**：重新打开 Settings → Plugins → 「聚合搜索」，卡片从字符串重建
   （开页即 refetch；配置缓存按 mtime/ino/size 失效，agent 直改后开页可见）。
2. **保存即热载**：任何修改都会立即序列化写回 `settings` 树并回读。
3. **桌面半（desktop/plugin.js）**：安装/更新/卸载后由宿主 reconcile 到
   `~/.hermes/desktop-plugins/<id>/`；加载/热载失败会弹错误 toast，兜底是
   ⌘K →「Reload desktop plugins」。
4. **搜索行为**：新配置在下一次搜索生效（进程内配置缓存按签名失效）；
   可用既有探针观察（`evidence/switch/probe_federated.py`，fresh 进程实读配置）。

## 界面上的两级勾选

- L1「参与聚合」= 该线路是否为 `backends` 成员；L2「参与」= 该 env 名是否为该线
  `credentials` 成员。取消勾选 = 从字符串移除（没有私有状态，字符串是唯一真源）。
- 卡片编辑器只写 `backends/timeout/max_results/min_backends/ranking_mode/pool_strategy/ranker`
  七个 schema 键；线路对象里 UI 不认识的扩展键（如 `api_key_env`、`required`）原样保留。
