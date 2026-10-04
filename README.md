# 飞控地面工具 · 第三方诊断类字节码复核

审查员在网页粘贴 **单个 Base64 编码的 JVM class 文件（≤ 64 KiB）** 并提交；
后端自行解析 class 常量池与 Code 属性，按 JVM 验证的类型推断（工作队列）
在正常边与异常边上传播类型状态，返回：

- 目标静态 `()V` 方法的**逐偏移**指令、入栈状态（底→顶）、栈高、局部变量；
- **异常处理器入口状态**（入口栈与清洗后的局部变量）；
- **通过**结论，或**首个拒绝证据**（阶段 + 稳定字节偏移 + 原因）。

## 安全策略（半初始化对象）

- `new` 产生的对象以 `uninitialized(new@<pc>:<class>)` 身份追踪，身份是
  产生它的那条 `new` 指令。
- `invokespecial <init>` 的接收者必须是某个 `new` 身份；构造成功后，栈与
  局部变量中同一身份的所有别名同时变为已初始化引用；对已初始化接收者二次
  构造被拒绝。
- 未初始化对象不得 `athrow`、`checkcast`、`instanceof`、`aastore` 或用于
  monitor 操作。
- **异常边**进入处理器时栈被重建为恰好一个异常引用（catch 类型，或
  finally 的 `Throwable`），帧内局部变量中所有未初始化槽位清洗为 `top` —
  半初始化对象既不能经栈、也不能经局部变量进入处理器。
- 未初始化身份不得与已初始化引用或另一条 `new` 的身份汇合；向后边不得
  携带未初始化对象（一条 `new` 只能构造一次）。
- 汇合时栈高必须一致、逐槽类型必须兼容。

## 支持范围

静态 `()V` 方法：分支（含 `tableswitch`/`lookupswitch`）、`new`、对象数组、
`invokespecial <init>`（参数支持引用/int/float）、局部变量读写、异常表、
整数与浮点运算、`dup*`/`pop*`/`swap` 等。无字段访问、无 category-2（long/double）、
无子例程（jsr/ret）、无非 void 返回、无其它 invoke。

## 稳定定位的拒绝

截断（含截断属性）、非法 magic、跳入指令中部、处理器范围非法（`start>=end`、
越界、边界/handler_pc 不在指令起点）、栈高下溢/不一致、类型冲突、未初始化
对象逃逸、工作队列不收敛——全部带字节偏移（解析阶段为 class 文件偏移，
验证阶段为 Code 内 pc）。服务无状态，每次提交重新解析验证，旧结论不残留。

## 本地运行（无第三方依赖，Python 3.11+ 标准库）

```bash
python3 -m app.main --port 8080
# GET  /            复核页
# GET  /healthz     {"status":"ok"}
# POST /api/verify  {"class_base64":"...","method":"verify"}
```

一键测试 + API/HTTP 冒烟：

```bash
python3 -m app.verify                      # 单元测试 + 进程内 API 冒烟
WEB_HEALTH_URL=http://127.0.0.1:8080 python3 -m app.verify   # 再加 HTTP 冒烟
python3 -m unittest discover -s tests -v   # 仅测试
```

## Compose

```bash
HOST_PORT=8080 docker compose up --build
```

- `web`：长驻复核页/API/健康检查，从配置的宿主端口 `${HOST_PORT:-8080}` 访问。
- `verify`：**一次性**服务，等待 `web` 健康后运行全部单元测试、镜像内
  API 冒烟与对 `http://web:8080` 的 HTTP/API 冒烟，完成即退出，状态码报告结果：

```bash
docker compose build
docker compose up --abort-on-container-exit --exit-code-from verify
echo $?   # 0 = 全部通过
```

## 布局

```
app/classfile.py   常量池/Code 严格解析（截断偏移）
app/descriptors.py 描述子解析
app/verifier.py    指令解码 + 工作队列数据流（正常边/异常边）
app/api.py         Base64/64KiB 边界与复核入口
app/server.py      stdlib HTTP 服务
app/webui.py       复核页
app/verify.py      一次性 verify 服务入口
tests/             class 手工构造器与 30 个测试
```
