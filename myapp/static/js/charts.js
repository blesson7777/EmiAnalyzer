(function () {
    function themePalette(isAdmin, size) {
        const userColors = ['#0f4c81', '#19a974', '#f59e0b', '#dc3545', '#2563eb', '#10b981'];
        const adminColors = ['#6a2e35', '#c1772f', '#0e7490', '#16a34a', '#ea580c', '#3b82f6'];
        const colors = isAdmin ? adminColors : userColors;
        return Array.from({ length: size }, (_, i) => colors[i % colors.length]);
    }

    function getCanvasContext(id) {
        const canvas = document.getElementById(id);
        return canvas ? canvas.getContext('2d') : null;
    }

    function amountLabel(value) {
        const numeric = Number(value || 0);
        if (Math.abs(numeric) >= 100000) {
            return `Rs. ${(numeric / 100000).toFixed(1)}L`;
        }
        return `Rs. ${numeric.toLocaleString()}`;
    }

    function defaultLegend() {
        return {
            position: 'bottom',
            labels: {
                usePointStyle: true,
                pointStyle: 'circle',
                boxWidth: 9,
                padding: 16,
            },
        };
    }

    window.renderFinancialCharts = function renderFinancialCharts(payload) {
        if (!payload) {
            return;
        }

        const isAdmin = document.body.classList.contains('mf-theme-admin') || document.body.classList.contains('admin-theme');
        const colors = themePalette(isAdmin, 10);

        const pieCtx = getCanvasContext('emiPieChart');
        if (pieCtx) {
            new Chart(pieCtx, {
                type: 'doughnut',
                data: {
                    labels: payload.emi_distribution.labels,
                    datasets: [{
                        data: payload.emi_distribution.values,
                        backgroundColor: themePalette(isAdmin, payload.emi_distribution.values.length),
                        borderWidth: 2,
                        borderColor: '#ffffff',
                        hoverOffset: 10,
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    cutout: '56%',
                    animation: {
                        duration: 1100,
                        easing: 'easeOutQuart',
                    },
                    plugins: {
                        legend: defaultLegend(),
                        tooltip: {
                            callbacks: {
                                label(context) {
                                    return `${context.label}: ${amountLabel(context.raw)}`;
                                },
                            },
                        },
                    },
                },
            });
        }

        const barCtx = getCanvasContext('cashflowBarChart');
        if (barCtx) {
            const cashflowValues = Array.isArray(payload.cashflow.values) ? payload.cashflow.values : [];
            const minCashflow = Math.min(0, ...cashflowValues);
            const maxCashflow = Math.max(0, ...cashflowValues);
            new Chart(barCtx, {
                type: 'bar',
                data: {
                    labels: payload.cashflow.labels,
                    datasets: [{
                        label: 'Amount',
                        data: payload.cashflow.values,
                        backgroundColor: themePalette(isAdmin, payload.cashflow.values.length),
                        borderRadius: 12,
                        maxBarThickness: 46,
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: {
                        duration: 1300,
                        easing: 'easeOutBack',
                    },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label(context) {
                                    return amountLabel(context.raw);
                                },
                            },
                        },
                    },
                    scales: {
                        y: {
                            beginAtZero: minCashflow >= 0,
                            suggestedMin: minCashflow < 0 ? Math.floor(minCashflow * 1.15) : 0,
                            suggestedMax: maxCashflow > 0 ? Math.ceil(maxCashflow * 1.15) : 0,
                            ticks: {
                                callback(value) {
                                    return amountLabel(value);
                                },
                            },
                            grid: {
                                color: 'rgba(148, 163, 184, 0.24)',
                            },
                        },
                        x: {
                            grid: { display: false },
                        },
                    },
                },
            });
        }

        const lineCtx = getCanvasContext('loanTimelineChart');
        if (lineCtx) {
            const gradient = lineCtx.createLinearGradient(0, 0, 0, 260);
            gradient.addColorStop(0, isAdmin ? 'rgba(106, 46, 53, 0.35)' : 'rgba(15, 76, 129, 0.32)');
            gradient.addColorStop(1, 'rgba(255, 255, 255, 0.03)');

            new Chart(lineCtx, {
                type: 'line',
                data: {
                    labels: payload.loan_timeline.labels,
                    datasets: [{
                        label: 'Remaining Loan Balance',
                        data: payload.loan_timeline.values,
                        borderColor: isAdmin ? '#6a2e35' : '#0f4c81',
                        backgroundColor: gradient,
                        tension: 0.34,
                        fill: true,
                        pointRadius: 4,
                        pointHoverRadius: 7,
                        pointBackgroundColor: '#f8fafc',
                        pointBorderWidth: 2,
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: {
                        duration: 1250,
                        easing: 'easeOutCubic',
                    },
                    plugins: {
                        legend: defaultLegend(),
                        tooltip: {
                            callbacks: {
                                label(context) {
                                    return amountLabel(context.raw);
                                },
                            },
                        },
                    },
                    scales: {
                        y: {
                            beginAtZero: true,
                            ticks: {
                                callback(value) {
                                    return amountLabel(value);
                                },
                            },
                            grid: {
                                color: 'rgba(148, 163, 184, 0.24)',
                            },
                        },
                        x: {
                            grid: { display: false },
                        },
                    },
                },
            });
        }
    };
})();
