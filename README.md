**提醒：本项目全部在 ChatGPT Work 中生成，是许愿式编程的产物。**

# Campus Wi-Fi

让 GL.iNet MT3600BE 路由器根据 Wi-Fi 连接情况登录校园网。

程序每隔一段时间读取一次 Wi-Fi 关联数量：

- 数量增加时尝试登录，例如 0→1、0→2、2→3。
- 数量不变或减少时不执行认证操作。
- 第一次检测只记录数量。
- 短按路由器重置键也可以触发登录。

默认检测间隔是 5 秒。有线设备不计入数量。

本项目只在 GL.iNet MT3600BE 原厂 OpenWrt 21.02 和当前 ePortal 接口上验证过。学校网线必须连接 WAN 口 `eth0`。认证服务器默认是 `http://10.255.100.2`，配置时可以修改。

## 安装

先下载并解压本项目。以下命令在电脑终端执行。

将路由器地址、SSH 端口和解压目录改成自己的实际值：

```sh
router_ip=192.168.8.1
router_port=22
cd /你的解压目录/campus-wifi
ssh -p "$router_port" root@"$router_ip" "mkdir -p /tmp/campus-wifi"
scp -P "$router_port" -O -r ./* root@"$router_ip":/tmp/campus-wifi/
ssh -p "$router_port" root@"$router_ip"
```

输入的是路由器管理密码。进入路由器终端后执行：

```sh
opkg update
opkg install python3
sh /tmp/campus-wifi/install.sh
python3 /usr/lib/campus-wifi/campus_wifi.py configure
```

按照提示填写校园网账号、密码和认证服务器地址。服务名称通常直接按回车留空，认证服务器地址直接按回车使用默认值。输入密码时屏幕不显示字符，这是正常现象。

账号、密码和认证服务器地址保存在路由器的 `/etc/campus-wifi/config.json`，请勿把该文件上传或发给别人。

## 测试并启动

如果账号已经在其他页面登录，请先退出。然后执行：

```sh
python3 /usr/lib/campus-wifi/campus_wifi.py login
```

看到“登录成功”后启动后台服务：

```sh
/etc/init.d/campus-wifi enable
/etc/init.d/campus-wifi start
```

服务刚启动时，已有 Wi-Fi 连接只会成为数量基准。以后检测到关联数量增加时才会尝试登录。需要立即登录时，直接执行上面的 `login` 命令。

## 手动命令

这些命令在后台服务运行或停止时都能使用：

```sh
# 登录校园网
python3 /usr/lib/campus-wifi/campus_wifi.py login

# 主动退出校园网
python3 /usr/lib/campus-wifi/campus_wifi.py logout

# 查看当前 Wi-Fi 关联情况
python3 /usr/lib/campus-wifi/campus_wifi.py status
```

手动命令不会改变程序记录的 Wi-Fi 数量基准。网络较慢时请等待结果，不要连续重复执行。

## 使用实体按钮登录

先绑定一次：

```sh
python3 /usr/lib/campus-wifi/bind_reset.py enable
```

以后轻按路由器重置键并在一秒内松开，即可尝试登录。

不要长按重置键。长按仍执行路由器原有的网络重置或恢复出厂设置功能。

取消绑定：

```sh
python3 /usr/lib/campus-wifi/bind_reset.py disable
```

## 查看日志

```sh
logread -e campus
```

同一个持续错误第一次立即记录，之后每 10 分钟最多重复一次。恢复正常后再次发生时会立即记录，因此默认每 5 秒检测也不会因同一个错误持续刷屏。

## 更多操作

- [修改检测间隔、升级、更换账号和暂停服务](docs/常用操作.md)
- [彻底卸载](docs/卸载.md)
- [工作原理和限制](docs/详细说明.md)
