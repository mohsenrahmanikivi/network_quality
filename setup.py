from setuptools import setup

package_name = 'network_quality'

setup(
    name=package_name,
    version='0.1.0',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='You',
    maintainer_email='you@example.com',
    description='Network quality monitoring node',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'network_quality_node = network_quality.network_quality_node:main',
            'net_echo_server = network_quality.net_echo_server:main',
            'net_echo_client = network_quality.net_echo_client:main',
        ],
    },
)
