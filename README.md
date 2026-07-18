# beautify.mp

JuneOver24 小程序的公开应急 H5 **加密发布仓库**。仓库保持 Public 以使用 GitHub
Pages，但不再保存自研明文源码。

## 发布内容

- `site/releases/<releaseId>/cipher/`：AES-256-GCM 自研资产密文。
- `site/releases/<releaseId>/public/`：固定版本第三方运行时和许可证明文。
- `site/releases/<releaseId>/manifest.json`：ECDSA 签名的资产图、摘要和 RSA recipients。
- `site/index.html`：不含业务算法的最小 Gateway 跳转页。

正式页面由独立 `beautify-asset-gateway` 返回。Gateway 验证 manifest 签名，使用
Cloudflare Secret 中的 RSA-OAEP 私钥解封装发布密钥，再按普通 HTML/JS/Worker URL
返回资源。主 FastAPI 失联不会影响该网关。

## 安全边界

该设计保护公开 Git 历史和 Pages 源站中的静态存储，防止直接浏览或克隆自研源码。
浏览器运行时仍会收到解密后的代码，因此 DevTools、代理或 Hook 可以提取运行代码；
它不是 DRM，也不能作为用户权限证明。

任何 RSA 私钥、ECDSA 签名私钥、Cloudflare/Tencent API 凭据、数据库配置或业务签名
密钥都不得进入本仓库。发布由私有 `JuneOver24Community` 构建流水线完成。

## 公开仓校验

```powershell
$env:BEAUTIFY_MP_VALIDATE_SNAPSHOT='1'
npm test
```

公开仓校验只验证已提交的密文快照。明文构建、第三方拆包、加密和签名只能在私有主仓
执行；所需私钥通过受保护环境文件或外部 signer 注入。
