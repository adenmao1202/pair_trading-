import numpy as np
import pandas as pd
from pathlib import Path
from typing import List, Tuple, Dict, Optional
import pandas_market_calendars as mcal
from datetime import datetime
from statsmodels.tsa.stattools import coint
from sklearn.linear_model import LinearRegression
import concurrent.futures
from dataclasses import dataclass


@dataclass
class PairTradingResult:
    pair: Tuple[str, str]
    start_date: str
    end_date: str
    positions: pd.DataFrame
    returns: pd.Series
    metrics: Dict
    exposures: pd.DataFrame  # Added to track position exposures


#################################################################################################
class PairsDataProcessor:  # checked  
    def __init__(self, data_folder: Path):
        self.data_folder = data_folder
        
    def load_stock_data(self, stock_code: str, file_path: Path) -> Optional[pd.DataFrame]:
        try:
            stock_df = pd.read_csv(
                file_path,
                parse_dates=['ts'],
                usecols=['ts', 'Close'],
                dtype={'Close': 'float32'}
            )
            
            if stock_df.empty:
                return None
                
            stock_df.set_index('ts', inplace=True)
            return stock_df
            
        except Exception as e:
            print(f"Error loading {stock_code}: {str(e)}")
            return None
    
    def resample_to_daily(self, 
                         stock_df: pd.DataFrame,
                         trading_days: pd.DatetimeIndex) -> Optional[pd.DataFrame]:
        try:
            if stock_df is None or stock_df.empty:
                return None
            
            daily_df = stock_df.resample('D').last()
            daily_df = daily_df.reindex(trading_days)
            return daily_df
            
        except Exception:
            return None
    
    def combine_stock_data(self, start_date: str, end_date: str) -> pd.DataFrame:
        all_stocks_daily = {}
        
        trading_days = mcal.get_calendar('XTAI').schedule(
            start_date=start_date, 
            end_date=end_date
        ).index
        
        csv_files = list(self.data_folder.glob('*.csv'))
        total_files = len(csv_files)
        print(f"Found {total_files} CSV files")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_stock = {
                executor.submit(self.load_and_process_stock, csv_file, trading_days): csv_file.stem
                for csv_file in csv_files
            }
            
            completed = 0
            for future in concurrent.futures.as_completed(future_to_stock):
                stock_code = future_to_stock[future]
                completed += 1
                
                try:
                    result = future.result()
                    if result is not None:
                        all_stocks_daily[stock_code] = result
                        
                    if completed % 10 == 0:
                        progress = (completed / total_files) * 100
                        print(f"Loading progress: {progress:.1f}% ({completed}/{total_files} files)")
                        
                except Exception as e:
                    print(f"Error processing {stock_code}: {str(e)}")
                    continue
        
        print(f"\nSuccessfully loaded {len(all_stocks_daily)} stocks")
        
        df = pd.DataFrame(all_stocks_daily)
        threshold = len(df.columns) * 0.5
        df = df.dropna(thresh=threshold)
        df = df.fillna(method='ffill').fillna(method='bfill')
        
        return df
    
    def load_and_process_stock(self, csv_file: Path, trading_days: pd.DatetimeIndex) -> Optional[pd.Series]:
        stock_df = self.load_stock_data(csv_file.stem, csv_file)
        if stock_df is None:
            return None
            
        daily_df = self.resample_to_daily(stock_df, trading_days)
        if daily_df is None:
            return None
            
        return daily_df['Close']



#########################################################################

class PairsTradingStrategy:
    def __init__(self, 
                 lookback_period: int = 60,   # 三個月的計算週期 for spread zscore : 這邊需要改進嗎？
                 enter_long_zscore_threshold: float = 1.5,
                 enter_short_zscore_threshold: float = 1.5,
                 exit_long_zscore_threshold: float = 0.5, 
                 exit_short_zscore_threshold: float = 0.5, 
                 min_samples: int = 252,  # 確保有一年資料 
                 coint_pvalue: float = 0.08,   
                 min_correlation: float = 0.7):   # checked 
        self.lookback_period = lookback_period
        self.enter_long_zscore_threshold = enter_long_zscore_threshold
        self.enter_short_zscore_threshold = enter_short_zscore_threshold
        self.exit_long_zscore_threshold = exit_long_zscore_threshold
        self.exit_short_zscore_threshold = exit_short_zscore_threshold
        self.min_samples = min_samples
        self.coint_pvalue = coint_pvalue
        self.min_correlation = min_correlation
    
    def prepare_pair_data(self, y: pd.Series, x: pd.Series) -> Tuple[pd.Series, pd.Series]:    # checked
        # ~ negates functions after,  making it True where values are " not NaN. "
        mask = ~np.isnan(y) & ~np.isnan(x)
        return y[mask], x[mask]
    
    def calculate_hedge_ratio(self, y: pd.Series, x: pd.Series) -> float:  # Beta in coint pairs  # checked 
        """ we might change the hedge ratio ( beta) cal method in future """
        return np.cov(y, x)[0, 1] / np.var(x)
    
    def calculate_zscore(self, spread: pd.Series) -> pd.Series:   # checked --> 這邊再確認一下為什麼要這樣計算 
        mean = pd.Series(index=spread.index, dtype='float32')
        std = pd.Series(index=spread.index, dtype='float32')
        
        for i in range(self.lookback_period, len(spread)):  
            window = spread.iloc[i-self.lookback_period:i]
            mean.iloc[i] = window.mean()
            std.iloc[i] = window.std()
            
        return (spread - mean) / std
    
    def generate_signals(self, zscore: pd.Series) -> pd.Series:  # checked
        """Enhanced signal generation with position tracking"""
        signals = pd.Series(0, index=zscore.index)
        position = 0
        
        for i in range(len(zscore)):
            if position == 0:  # No position
                if zscore.iloc[i] < -self.enter_long_zscore_threshold:
                    position = 1
                    signals.iloc[i] = 1
                elif zscore.iloc[i] > self.enter_short_zscore_threshold:
                    position = -1
                    signals.iloc[i] = -1
            elif position == 1:  # Long position
                if zscore.iloc[i] >= self.exit_long_zscore_threshold:
                    position = 0
                    signals.iloc[i] = 0
            elif position == -1:  # Short position
                if zscore.iloc[i] <= -self.exit_short_zscore_threshold:
                    position = 0
                    signals.iloc[i] = 0
                    
        return signals
    
    
    # 這邊更改手續費 
    def calculate_returns(self, 
                         pair_data: pd.DataFrame, 
                         signals: pd.Series, 
                         hedge_ratio: float,
                         transaction_cost: float = 0.001) -> pd.Series:   #  checked 

        # Calculate returns for both stocks
        stock1_rets = pair_data.iloc[:, 0].pct_change()
        stock2_rets = pair_data.iloc[:, 1].pct_change()
        
        # Calculate position changes for later use of transaction costs
        pos_changes = signals.diff().fillna(0)
        
        # Calculate notional exposure for each leg
        stock1_notional = 1.0
        stock2_notional = hedge_ratio
        total_notional = abs(stock1_notional) + abs(stock2_notional)
        
        # weight for two stocks: actual position 
        # for multi pairs in future, change here 
        stock1_weight = stock1_notional / total_notional
        stock2_weight = stock2_notional / total_notional
        
        # Calculate strategy returns: 
        """ 每一次交易，為了對沖風險，一定是多空一組一起做 """
        strategy_rets = signals.shift(1) * (
            stock1_weight * stock1_rets - 
            stock2_weight * stock2_rets
        )
        
        # Apply transaction costs to both legs
        transaction_costs = abs(pos_changes) * transaction_cost * (
            abs(stock1_weight) + abs(stock2_weight)
        )
        
        strategy_rets = strategy_rets - transaction_costs
        
        # Store additional information
        strategy_rets.attrs['position_changes'] = pos_changes
        strategy_rets.attrs['transaction_costs'] = transaction_costs
        strategy_rets.attrs['weights'] = {'stock1': stock1_weight, 'stock2': stock2_weight}
        
        return strategy_rets
    
    
    def calculate_position_exposures(self,
                                   pair_data: pd.DataFrame,
                                   signals: pd.Series,
                                   hedge_ratio: float) -> pd.DataFrame:   # cheked 
        """
        1. Gross_exposure : Larger gross exposure means more liquidity is needed to open and close the position, 
        increasing the risk of slippage (adverse price movements during trade execution).
        
        2. Net_exposure :  This value shows the overall directional bias of the position, 
        indicating whether the pair trade is net long or net short in dollar terms.
        """
        stock1_notional = 1.0
        stock2_notional = hedge_ratio
        total_notional = abs(stock1_notional) + abs(stock2_notional)
        
        stock1_weight = stock1_notional / total_notional
        stock2_weight = stock2_notional / total_notional
        
        stock1_position = signals * stock1_weight  #  long leg
        stock2_position = -signals * stock2_weight #  short leg 
        
        stock1_exposure = stock1_position * pair_data.iloc[:, 0]
        stock2_exposure = stock2_position * pair_data.iloc[:, 1]
        
        return pd.DataFrame({
            'stock1_position': stock1_position,
            'stock2_position': stock2_position,
            'stock1_exposure': stock1_exposure,
            'stock2_exposure': stock2_exposure,
            'net_exposure': stock1_exposure + stock2_exposure,  
            'gross_exposure': abs(stock1_exposure) + abs(stock2_exposure)  
        })

    
    def calculate_metrics(self, returns: pd.Series) -> Dict:  # checked 
        """returns : daily return in pct form """
        annual_factor = 252
        
        # Basic return metrics
        total_return = np.expm1(np.sum(np.log1p(returns)))  # use log return 
        annual_return = np.expm1(np.sum(np.log1p(returns)) * annual_factor / len(returns))
        annual_volatility = returns.std() * np.sqrt(annual_factor)
        sharpe_ratio = np.sqrt(annual_factor) * returns.mean() / returns.std() if returns.std() != 0 else 0  # annualized mean(ret) / annualized std = 252.root * daily ret / daily std 
        
        
        # Win rate metrics
        winning_trades = (returns > 0).sum()
        total_trades = (~returns.isna()).sum()
        win_rate = winning_trades / total_trades if total_trades > 0 else 0
        
        # PnL statistics
        cumulative_returns = (1 + returns).cumprod() - 1
        drawdowns = cumulative_returns - cumulative_returns.cummax()
        max_drawdown = drawdowns.min()
        
        # Trade metrics
        avg_win = returns[returns > 0].mean() if len(returns[returns > 0]) > 0 else 0
        avg_loss = returns[returns < 0].mean() if len(returns[returns < 0]) > 0 else 0
        profit_factor = abs(returns[returns > 0].sum() / returns[returns < 0].sum()) if len(returns[returns < 0]) > 0 else np.inf
        # Ratio of the sum of profits to the sum of losses. It’s a measure of profitability.

        daily_pnl = returns.sum() 
        avg_daily_pnl = returns.mean()
        
        # Streak analysis
        streak = (returns > 0).astype(int)
        streak = streak.map({1: 1, 0: -1})
        
        pos_streaks = (streak > 0).astype(int)
        neg_streaks = (streak < 0).astype(int)
        
        pos_streak_groups = (pos_streaks != pos_streaks.shift()).cumsum()
        neg_streak_groups = (neg_streaks != neg_streaks.shift()).cumsum()
        
        longest_win_streak = pos_streaks.groupby(pos_streak_groups).sum().max() if len(pos_streaks) > 0 else 0
        longest_lose_streak = neg_streaks.groupby(neg_streak_groups).sum().max() if len(neg_streaks) > 0 else 0
        
        return {
            'total_return': total_return,
            'annual_return': annual_return,
            'annual_volatility': annual_volatility,
            'sharpe_ratio': sharpe_ratio,
            'win_rate': win_rate,
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': total_trades - winning_trades,
            'total_pnl': daily_pnl,
            'average_daily_pnl': avg_daily_pnl,
            'average_win': avg_win,
            'average_loss': avg_loss,
            'profit_factor': profit_factor,
            'max_drawdown': max_drawdown,
            'longest_win_streak': longest_win_streak,
            'longest_lose_streak': longest_lose_streak
        }
    
    
    def check_pair_validity(self, y: pd.Series, x: pd.Series) -> bool:   # checked 
        if len(y) < self.min_samples:  # Minimum number of samples
            return False
            
        correlation = np.corrcoef(y, x)[0, 1]  # Check correlation > threshold 
        if abs(correlation) < self.min_correlation:
            return False
            
        try:
            _, p_value, _ = coint(y, x)  # Check for cointegration 
            return p_value <= self.coint_pvalue
        except:
            return False

    def execute_pair_trade(self, 
                      stock1_data: pd.Series, 
                      stock2_data: pd.Series,
                      pair: Tuple[str, str]) -> Optional[PairTradingResult]:
        """
        Func : Execute pairs trading strategy for a given pair of stocks
        
        """
        try:
            # Use prepare_pair_data to mask out Nan dataset 
            stock1_clean, stock2_clean = self.prepare_pair_data(stock1_data, stock2_data)   # after masking out Nan, there should be no Nan in data 
            
            # check if datas are empty 
            if stock1_clean.empty or stock2_clean.empty:   
                print(f"Empty data for pair {pair}")
                return None
            
            
            # check if lenght is equal 
            if len(stock1_clean) != len(stock2_clean):   
                print(f"Mismatched data lengths for pair {pair}")
                return None
            
            # Check pair validity (coint, corr, min sample )
            if not self.check_pair_validity(stock1_clean, stock2_clean):
                return None
            
            # Calculate hedge ratio and spread
            # spread alread includes Beta cal by hedge ratio 
            hedge_ratio = self.calculate_hedge_ratio(stock1_clean, stock2_clean)
            spread = stock1_clean - hedge_ratio * stock2_clean
            
            # Calculate z-scores and generate trading signals
            zscore = self.calculate_zscore(spread)
            signals = self.generate_signals(zscore)
            
            # Prepare pair data for returns calculation
            pair_data = pd.concat([stock1_clean, stock2_clean], axis=1)
            pair_data.columns = [f"{pair[0]}_price", f"{pair[1]}_price"]
            
            # Calculate returns and exposures
            returns = self.calculate_returns(pair_data, signals, hedge_ratio)
            exposures = self.calculate_position_exposures(pair_data, signals, hedge_ratio)
            
            # Calculate trade statistics
            trade_changes = signals.diff().fillna(0)
            trade_entries = trade_changes != 0
            trade_count = trade_entries.sum()
            
            if trade_count == 0:
                print(f"No trades generated for pair {pair}")
                return None
            
            # Calculate metrics
            metrics = self.calculate_metrics(returns.dropna())
            
            # Add trade statistics to metrics
            metrics.update({
                'number_of_trades': trade_count,
                'avg_trade_duration': len(signals) / trade_count if trade_count > 0 else 0,
                'hedge_ratio': hedge_ratio,
                'spread_stdev': spread.std(),
                'correlation': np.corrcoef(stock1_clean, stock2_clean)[0, 1]
            })
            
            # Create enhanced positions DataFrame
            positions = pd.DataFrame({
                'signals': signals,
                'zscore': zscore,
                'spread': spread,
                'stock1_price': pair_data.iloc[:, 0],
                'stock2_price': pair_data.iloc[:, 1],
                'stock1_position': exposures['stock1_position'],
                'stock2_position': exposures['stock2_position'],
                'net_exposure': exposures['net_exposure']
            })
            
            # Add trade markers
            positions['trade_entry'] = trade_entries
            positions['trade_exit'] = trade_changes != 0
            
            # Calculate drawdown series
            cumulative_returns = (1 + returns).cumprod()
            drawdown_series = cumulative_returns / cumulative_returns.cummax() - 1
            positions['drawdown'] = drawdown_series
            
            # Create result object with enhanced information
            result = PairTradingResult(
                pair=pair,
                start_date=stock1_clean.index[0].strftime('%Y-%m-%d'),
                end_date=stock1_clean.index[-1].strftime('%Y-%m-%d'),
                positions=positions,
                returns=returns,
                metrics=metrics,
                exposures=exposures
            )
            
            # Add additional attributes for analysis
            result.hedge_ratio = hedge_ratio
            result.spread_mean = spread.mean()
            result.spread_std = spread.std()
            result.zscore_mean = zscore.mean()
            result.zscore_std = zscore.std()
            result.trade_count = trade_count
            result.drawdown_series = drawdown_series
            
            # Log successful execution
            print(f"Successfully processed pair {pair} with {trade_count} trades")
            print(f"Sharpe Ratio: {metrics['sharpe_ratio']:.2f}, "
                f"Total Return: {metrics['total_return']:.2%}, "
                f"Win Rate: {metrics['win_rate']:.2%}")
            
            return result
            
        except Exception as e:
            print(f"Error processing pair {pair}: {str(e)}")
            import traceback
            traceback.print_exc()
            return None
            
        
        
