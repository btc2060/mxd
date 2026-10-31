CREATE TABLE IF NOT EXISTS preferences (
 id INT PRIMARY KEY, interval_seconds INT NOT NULL, enabled BOOLEAN NOT NULL, max_pages INT NOT NULL,
 engine VARCHAR(16) NOT NULL, merchant_selector VARCHAR(400) NOT NULL, player_selector VARCHAR(400) NOT NULL,
 next_due_at DATETIME(3) NULL, last_manual_at DATETIME(3) NULL, requested BOOLEAN NOT NULL, failure_streak INT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS collector_lease (
 id INT PRIMARY KEY, owner VARCHAR(64) NOT NULL, expires_at DATETIME(3) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS crawl_runs (
 id INT AUTO_INCREMENT PRIMARY KEY, dataset VARCHAR(8) NOT NULL, started_at DATETIME(3) NOT NULL,
 finished_at DATETIME(3) NULL, status VARCHAR(16) NOT NULL, origin VARCHAR(20) NOT NULL, engine VARCHAR(20) NOT NULL,
 pages INT NOT NULL, merchant_count INT NOT NULL, player_count INT NOT NULL, error TEXT NOT NULL,
 INDEX runs_dataset_time (dataset,started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS quotes (
 id INT AUTO_INCREMENT PRIMARY KEY, run_id INT NOT NULL, side VARCHAR(12) NOT NULL, source_key VARCHAR(64) NOT NULL,
 title VARCHAR(300) NOT NULL, source_url TEXT NOT NULL, wan_per_yuan DECIMAL(24,12) NOT NULL,
 yuan_per_wan DECIMAL(24,12) NOT NULL, quantity_wan DECIMAL(24,4) NULL, evidence TEXT NOT NULL,
 UNIQUE KEY quote_identity (run_id,side,source_key), INDEX quote_lookup (run_id,side),
 FOREIGN KEY (run_id) REFERENCES crawl_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS snapshots (
 id INT AUTO_INCREMENT PRIMARY KEY, run_id INT NOT NULL, dataset VARCHAR(8) NOT NULL, side VARCHAR(12) NOT NULL,
 at DATETIME(3) NOT NULL, best DECIMAL(24,12) NOT NULL, median DECIMAL(24,12) NOT NULL,
 worst DECIMAL(24,12) NOT NULL, count INT NOT NULL,
 UNIQUE KEY snapshot_identity (run_id,side), INDEX snapshot_lookup (dataset,side,at),
 FOREIGN KEY (run_id) REFERENCES crawl_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
