# QuickForm MySQL 迁移简版（Windows / PowerShell）

这份文档只保留实操必需步骤：导出、建库授权、导入、校验、`.env` 配置。

## 1) 导出数据库（建议用 root）

先进入 MySQL `bin` 目录（或把该目录加到系统环境变量）：

```powershell
cd "C:\Program Files\MySQL\MySQL Server 9.5\bin"
```

推荐导出命令（不导出 GTID 和表空间）：

```powershell
mysqldump -u root -p --set-gtid-purged=OFF --no-tablespaces quickform > C:\quickform_backup.sql
```

## 2) 目标机器建库与授权

登录 MySQL：

```powershell
mysql -u root -p
```

执行：

```sql
CREATE DATABASE IF NOT EXISTS quickform CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
SHOW DATABASES;
GRANT ALL PRIVILEGES ON quickform.* TO 'linmy'@'%';
FLUSH PRIVILEGES;
```

## 3) （可选）清空重建数据库

```powershell
mysql -u linmy -p -e "DROP DATABASE IF EXISTS quickform; CREATE DATABASE quickform CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
```

## 4) 处理导出文件中的 GTID（如果需要）

如果已有旧备份带 GTID 行，可先生成无 GTID 文件：

```powershell
findstr /V /I "GTID_PURGED" C:\quickform_backup.sql > C:\quickform_backup_no_gtid.sql
```

## 5) 导入数据库

最简导入（推荐使用清理后的备份）：

```powershell
mysql -u linmy -p quickform < C:\quickform_backup_no_gtid.sql
```

如果遇到编码问题：

```powershell
mysql -u root -p --default-character-set=utf8mb4 quickform < C:\quickform_backup.sql
```

## 6) 导入后快速校验

```powershell
mysql -u linmy -p -e "USE quickform; SHOW TABLES; SELECT 'user' as table_name, COUNT(*) as row_count FROM user UNION ALL SELECT 'task', COUNT(*) FROM task UNION ALL SELECT 'submission', COUNT(*) FROM submission;"
```

## 7) QuickForm `.env` 配置

```env
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=linmy
MYSQL_PASSWORD=linmy
MYSQL_DATABASE=quickform
```

---

## 常见注意点（精简）

- `mysqldump` / `mysql` 找不到：确认在 MySQL `bin` 目录执行，或把 `bin` 加到系统变量。
- 导入报字符集问题：优先加 `--default-character-set=utf8mb4`。
- 导入前若要覆盖旧库：先执行“清空重建数据库”步骤。
