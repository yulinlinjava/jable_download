"""Lightweight Linux/Docker resource monitoring with three-day SQLite history."""
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import logging


log = logging.getLogger(__name__)


class SystemMonitor:
    RETENTION_SECONDS = 3 * 24 * 3600

    def __init__(self, download_root, database, runtime_provider=None):
        self.download_root = Path(download_root).resolve()
        self.database = Path(database).resolve()
        self.runtime_provider = runtime_provider or (lambda: [])
        self.interval = max(2, min(60, int(os.getenv('SYSTEM_METRICS_INTERVAL', '5'))))
        self.started_at = time.time()
        self._closed = threading.Event()
        self._lock = threading.RLock()
        self._current = {}
        self._processes = []
        self._previous_container_cpu = None
        self._previous_host_cpu = None
        self._previous_process_cpu = {}
        self._last_cleanup = 0
        self._clock_ticks = self._sysconf('SC_CLK_TCK', 100)
        self._page_size = self._sysconf('SC_PAGE_SIZE', 4096)
        self._initialize_database()
        self._sample()
        self._thread = threading.Thread(
            target=self._run, name='system-metrics-sampler', daemon=True)
        self._thread.start()

    @staticmethod
    def _sysconf(name, default):
        try:
            return int(os.sysconf(name))
        except (AttributeError, OSError, ValueError):
            return default

    def _db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _initialize_database(self):
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS system_metric_samples (
                          sampled_at INTEGER PRIMARY KEY,
                          cpu_percent REAL NOT NULL DEFAULT 0,
                          cpu_cores REAL NOT NULL DEFAULT 1,
                          memory_used INTEGER NOT NULL DEFAULT 0,
                          memory_limit INTEGER NOT NULL DEFAULT 0,
                          memory_percent REAL NOT NULL DEFAULT 0,
                          disk_used INTEGER NOT NULL DEFAULT 0,
                          disk_total INTEGER NOT NULL DEFAULT 0,
                          disk_percent REAL NOT NULL DEFAULT 0,
                          service_cpu_percent REAL NOT NULL DEFAULT 0,
                          service_memory INTEGER NOT NULL DEFAULT 0,
                          ffmpeg_cpu_percent REAL NOT NULL DEFAULT 0,
                          ffmpeg_memory INTEGER NOT NULL DEFAULT 0,
                          ffmpeg_count INTEGER NOT NULL DEFAULT 0)''')

    @staticmethod
    def _read_text(path):
        try:
            return Path(path).read_text(encoding='utf-8', errors='ignore').strip()
        except OSError:
            return None

    def _cpu_limit(self):
        value = self._read_text('/sys/fs/cgroup/cpu.max')
        host_cores = max(1, os.cpu_count() or 1)
        if value:
            parts = value.split()
            if len(parts) == 2 and parts[0] != 'max':
                try:
                    return max(0.01, float(parts[0]) / float(parts[1])), True
                except (ValueError, ZeroDivisionError):
                    pass
        return float(host_cores), False

    def _container_cpu(self, now, cores):
        cpu_stat = self._read_text('/sys/fs/cgroup/cpu.stat')
        if cpu_stat:
            values = {}
            for line in cpu_stat.splitlines():
                key, _, value = line.partition(' ')
                try:
                    values[key] = int(value)
                except ValueError:
                    continue
            usage = values.get('usage_usec')
            if usage is not None:
                previous = self._previous_container_cpu
                self._previous_container_cpu = (usage, now)
                if previous and now > previous[1]:
                    used_seconds = (usage - previous[0]) / 1_000_000
                    return max(0.0, min(100.0, used_seconds / (now - previous[1]) / cores * 100))
                return 0.0
        stat = self._read_text('/proc/stat')
        if stat:
            first = stat.splitlines()[0].split()[1:]
            try:
                numbers = [int(item) for item in first]
                total, idle = sum(numbers), numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
                previous = self._previous_host_cpu
                self._previous_host_cpu = (total, idle)
                if previous and total > previous[0]:
                    delta_total, delta_idle = total - previous[0], idle - previous[1]
                    return max(0.0, min(100.0, (delta_total - delta_idle) / delta_total * 100))
            except (ValueError, IndexError, ZeroDivisionError):
                pass
        return 0.0

    def _memory(self):
        current = self._read_text('/sys/fs/cgroup/memory.current')
        maximum = self._read_text('/sys/fs/cgroup/memory.max')
        if current and maximum:
            try:
                used = int(current)
                if maximum != 'max':
                    limit = int(maximum)
                    return used, limit, True
            except ValueError:
                pass
        info = self._read_text('/proc/meminfo')
        if info:
            values = {}
            for line in info.splitlines():
                key, _, raw = line.partition(':')
                try:
                    values[key] = int(raw.strip().split()[0]) * 1024
                except (ValueError, IndexError):
                    continue
            total = values.get('MemTotal', 0)
            available = values.get('MemAvailable', values.get('MemFree', 0))
            return max(0, total - available), total, False
        return 0, 0, False

    def _process_stat(self, pid, now, cores):
        raw = self._read_text(f'/proc/{pid}/stat')
        if not raw or ') ' not in raw:
            return 0.0, 0
        try:
            fields = raw.rsplit(') ', 1)[1].split()
            ticks = int(fields[11]) + int(fields[12])
            rss = int(fields[21]) * self._page_size
        except (ValueError, IndexError):
            return 0.0, 0
        previous = self._previous_process_cpu.get(pid)
        self._previous_process_cpu[pid] = (ticks, now)
        percent = 0.0
        if previous and now > previous[1]:
            cpu_seconds = (ticks - previous[0]) / self._clock_ticks
            percent = max(0.0, cpu_seconds / (now - previous[1]) / cores * 100)
        return round(percent, 2), max(0, rss)

    def _active_tasks(self):
        with self._db() as db:
            rows = db.execute("""SELECT id,file_name,status,stage,progress FROM ffmpeg_tasks
                               WHERE status NOT IN ('queued','completed','failed','cancelled')""").fetchall()
        return {row['id']: dict(row) for row in rows}

    def _ffmpeg_processes(self, now, cores):
        proc_root = Path('/proc')
        if not proc_root.is_dir():
            return []
        tasks = self._active_tasks()
        runtime = {int(item['pid']): item for item in self.runtime_provider()
                   if item.get('pid') is not None}
        processes = []
        live_pids = {os.getpid()}
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            command = self._read_text(entry / 'comm') or ''
            if command not in {'ffmpeg', 'ffprobe'}:
                continue
            live_pids.add(pid)
            cmdline = (self._read_text(entry / 'cmdline') or '').replace('\x00', ' ')
            current_runtime = runtime.get(pid, {})
            task_id = current_runtime.get('taskId')
            if not task_id:
                task_id = next((identifier for identifier in tasks if identifier in cmdline), None)
            task = tasks.get(task_id, {}) if task_id else {}
            cpu, memory = self._process_stat(pid, now, cores)
            processes.append({
                'pid': pid, 'command': command, 'taskId': task_id,
                'fileName': task.get('file_name'),
                'stage': current_runtime.get('stage') or task.get('stage'),
                'progress': task.get('progress'), 'cpuPercent': cpu,
                'memoryUsed': memory, 'startedAt': current_runtime.get('startedAt'),
                'frame': current_runtime.get('frame'), 'speed': current_runtime.get('speed'),
                'outTime': current_runtime.get('outTime'),
            })
        self._previous_process_cpu = {
            pid: value for pid, value in self._previous_process_cpu.items() if pid in live_pids}
        return processes

    def _sample(self):
        now_float = time.time()
        now = int(now_float)
        cores, cpu_limited = self._cpu_limit()
        cpu = self._container_cpu(now_float, cores)
        memory_used, memory_limit, memory_limited = self._memory()
        disk = shutil.disk_usage(self.download_root)
        service_cpu, service_memory = self._process_stat(os.getpid(), now_float, cores)
        processes = self._ffmpeg_processes(now_float, cores)
        ffmpeg_cpu = sum(item['cpuPercent'] for item in processes)
        ffmpeg_memory = sum(item['memoryUsed'] for item in processes)
        memory_percent = memory_used / memory_limit * 100 if memory_limit else 0
        disk_percent = disk.used / disk.total * 100 if disk.total else 0
        try:
            load_average = os.getloadavg()[0]
        except (AttributeError, OSError):
            load_average = 0.0
        current = {
            'sampledAt': now, 'cpuPercent': round(cpu, 2), 'cpuCores': round(cores, 2),
            'cpuLimited': cpu_limited, 'memoryUsed': memory_used,
            'memoryLimit': memory_limit, 'memoryPercent': round(memory_percent, 2),
            'memoryLimited': memory_limited, 'diskUsed': disk.used,
            'diskTotal': disk.total, 'diskFree': disk.free,
            'diskPercent': round(disk_percent, 2),
            'serviceCpuPercent': service_cpu, 'serviceMemory': service_memory,
            'ffmpegCpuPercent': round(ffmpeg_cpu, 2),
            'ffmpegMemory': ffmpeg_memory, 'ffmpegCount': len(processes),
            'loadAverage1m': round(load_average, 2),
            'serviceUptime': max(0, round(now_float - self.started_at)),
            'sampleInterval': self.interval, 'retentionDays': 3,
        }
        with self._lock:
            self._current = current
            self._processes = processes
        with self._db() as db:
            db.execute('''INSERT OR REPLACE INTO system_metric_samples(
                          sampled_at,cpu_percent,cpu_cores,memory_used,memory_limit,
                          memory_percent,disk_used,disk_total,disk_percent,
                          service_cpu_percent,service_memory,ffmpeg_cpu_percent,
                          ffmpeg_memory,ffmpeg_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                now, current['cpuPercent'], current['cpuCores'], memory_used, memory_limit,
                current['memoryPercent'], disk.used, disk.total, current['diskPercent'],
                service_cpu, service_memory, current['ffmpegCpuPercent'],
                ffmpeg_memory, len(processes)))
            if now - self._last_cleanup >= 3600:
                db.execute('DELETE FROM system_metric_samples WHERE sampled_at<?',
                           (now - self.RETENTION_SECONDS,))
                self._last_cleanup = now

    def _run(self):
        while not self._closed.wait(self.interval):
            try:
                self._sample()
            except Exception:
                # Monitoring must never interrupt downloads or API service startup.
                log.exception('System metrics sampling failed')

    def current(self):
        with self._lock:
            return dict(self._current)

    def ffmpeg_processes(self):
        with self._lock:
            return [dict(item) for item in self._processes]

    def history(self, minutes):
        minutes = max(1, min(3 * 24 * 60, int(minutes)))
        if minutes <= 60:
            bucket = self.interval
        elif minutes <= 360:
            bucket = 30
        elif minutes <= 1440:
            bucket = 120
        else:
            bucket = 300
        since = int(time.time()) - minutes * 60
        with self._db() as db:
            rows = db.execute('''SELECT (sampled_at / ?) * ? AS sampled_at,
                              avg(cpu_percent) AS cpu_percent,
                              avg(memory_percent) AS memory_percent,
                              avg(disk_percent) AS disk_percent,
                              avg(service_cpu_percent) AS service_cpu_percent,
                              avg(ffmpeg_cpu_percent) AS ffmpeg_cpu_percent,
                              avg(ffmpeg_memory) AS ffmpeg_memory,
                              avg(ffmpeg_count) AS ffmpeg_count
                              FROM system_metric_samples WHERE sampled_at>=?
                              GROUP BY (sampled_at / ?) ORDER BY sampled_at''',
                              (bucket, bucket, since, bucket)).fetchall()
        return {
            'minutes': minutes, 'bucketSeconds': bucket, 'retentionDays': 3,
            'items': [{
                'sampledAt': row['sampled_at'],
                'cpuPercent': round(row['cpu_percent'] or 0, 2),
                'memoryPercent': round(row['memory_percent'] or 0, 2),
                'diskPercent': round(row['disk_percent'] or 0, 2),
                'serviceCpuPercent': round(row['service_cpu_percent'] or 0, 2),
                'ffmpegCpuPercent': round(row['ffmpeg_cpu_percent'] or 0, 2),
                'ffmpegMemory': round(row['ffmpeg_memory'] or 0),
                'ffmpegCount': round(row['ffmpeg_count'] or 0, 2),
            } for row in rows],
        }

    def close(self):
        self._closed.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
